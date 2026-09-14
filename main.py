import os
import sys
import asyncio
import re
import html
import random
from datetime import datetime, timedelta
try:
    import pyotp
except ImportError:
    pyotp = None
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# 1. 环境变量配置解析
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

admin_id_env = os.environ.get("ADMIN_USER_ID", "").strip()
ADMIN_USER_IDS = set(
    int(x) for x in re.split(r'[,;\s]+', admin_id_env) if x.isdigit()
)

ADMIN_USER = os.environ.get("ADMIN_USER", "").strip()
ADMIN_PASS = os.environ.get("ADMIN_PASS", "").strip()

raw_admin_url = os.environ.get("ADMIN_URL", "").strip()
match = re.search(r'https?://[^\s\]\)\>\"\']+', raw_admin_url)
BASE_ADMIN_URL = match.group(0).rstrip('/') if match else raw_admin_url.rstrip('/')

# 单笔商城 / JJ 订单后台配置
SINGLE_ADMIN_USER = os.environ.get("SINGLE_ADMIN_USER", "").strip()
SINGLE_ADMIN_PASS = os.environ.get("SINGLE_ADMIN_PASS", "").strip()

raw_single_admin_url = os.environ.get("SINGLE_ADMIN_URL", "").strip()
match_single = re.search(r'https?://[^\s\]\)\>\"\']+', raw_single_admin_url)
SINGLE_ADMIN_URL = match_single.group(0).rstrip('/') if match_single else raw_single_admin_url.rstrip('/')
# 登录地址是 /market_managers/sign_in；进入后台功能页时使用站点根路径。
SINGLE_ADMIN_ROOT = re.sub(r'/market_managers/sign_in/?$', '', SINGLE_ADMIN_URL, flags=re.IGNORECASE).rstrip('/')

JJ_ADMIN_USER = os.environ.get("JJ_ADMIN_USER", "").strip()
JJ_ADMIN_PASS = os.environ.get("JJ_ADMIN_PASS", "").strip()
JJ_2FA_SECRET = os.environ.get("JJ_2FA_SECRET", "").strip()

raw_jj_admin_url = os.environ.get("JJ_ADMIN_URL", "").strip()
match_jj = re.search(r'https?://[^\s\]\)\>\"\']+', raw_jj_admin_url)
JJ_ADMIN_URL = match_jj.group(0).rstrip('/') if match_jj else raw_jj_admin_url.rstrip('/')

MANAGER_RECEIVE_NAME = "管理员代收"

# 全局任务字典
ACTIVE_TASKS = {}

# 【建店专用排队锁】：同时只允许 1 个建店任务在后台运行，后续建店请求自动排队
BUILD_SHOP_SEMAPHORE = asyncio.Semaphore(1)

# 商城界面选项（与后台对应）
SKIN_OPTIONS = {
    "jisumeishang": "极速微商",
    "qimiao": "七喵",
    "qiyue": "柒月",
    "yinnierlai": "音你而来"
}


# 2. 文本解析与格式校验（全面优化简繁体兼容与格式判断）
def parse_and_validate_text(text: str) -> tuple[dict, str]:
    info = {}
    errors = []

    # 特征词定义
    digital_keywords = [
        "数字R人民币", "數字R人民幣", "数字R", "數字R",
        "数字人民币", "數字人民幣", "數位人民幣", "数位人民币",
        "数字名", "數字名", "数位名", "數位名", "数字户名", "數字戶名",
        "数币", "數幣", "数字", "數字", "数位", "數位",
        "钱包", "錢包", "ecny"
    ]
    bank_keywords = ["银行", "銀行", "开户行", "開戶行", "支行"]
    alipay_keywords = ["支付宝", "支付寶", "支付宝户名", "支付寶戶名", "支付宝名", "支付寶名"]

    # 优先判定整单类型
    if any(k in text for k in alipay_keywords):
        info["type"] = "alipay"
    elif any(k in text for k in digital_keywords):
        info["type"] = "digital_wallet"
    elif any(k in text for k in bank_keywords):
        info["type"] = "bank"
    else:
        info["type"] = "alipay"

    clean_text = re.sub(r'mailto:', '', text, flags=re.IGNORECASE)
    clean_text = re.sub(r'https?://[^\s]+', '', clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(r'<[^>]+>', '', clean_text)

    raw_accounts = {}
    raw_phone = None
    empty_fields = []

    base_ignore_keys = ["余额", "餘額", "状态", "狀態", "备注", "備註", "限制", "风控", "風控", "交易日"]

    lines = clean_text.splitlines()

    # 第一阶段：优先提取平台账号
    for line in lines:
        line = line.strip()
        if not line or any(ik in line for ik in base_ignore_keys):
            continue
        parts = re.split(r'[:：]', line, maxsplit=1)
        if len(parts) < 2:
            continue
        key = re.sub(r'\s+', '', parts[0])
        val = parts[1].strip()
        val = re.sub(r'^[<\("‘“]+|[>\)"”]+$', '', val)

        if "登入" not in key and (
            any(k in key for k in ["平台", "会员", "會員"])
            or key in ["平台账号", "平台帳號", "平台会员账号", "平台會員帳號", "会员账号", "會員帳號"]
        ):
            if not any(k in key for k in ["支付宝", "支付寶", "银行", "銀行", "数字", "數字", "数位", "數位", "钱包", "錢包"]):
                if val:
                    info["account"] = val.lower()
                    break

    # 第二阶段：提取各具体字段（增强简繁体兼容）
    for line in lines:
        line = line.strip()
        if not line:
            continue

        has_base_ignore = any(ik in line for ik in base_ignore_keys)
        has_order_and_last = ("订单" in line or "訂單" in line) and ("最后" in line or "最後" in line)
        if has_base_ignore or has_order_and_last:
            continue

        parts = re.split(r'[:：]', line, maxsplit=1)
        if len(parts) < 2:
            continue

        key = re.sub(r'\s+', '', parts[0])
        val = parts[1].strip()
        val = re.sub(r'^[<\("‘“]+|[>\)"”]+$', '', val)

        if any(ik in key for ik in base_ignore_keys):
            continue

        if not val:
            if not any(ik in key for ik in ["商城", "模板", "界面"]):
                empty_fields.append(parts[0].strip())
            continue

        # 匹配户名
        if any(k in key for k in [
            "户名", "戶名", "姓名", "名字", "客户姓名", "客戶姓名",
            "支付宝户名", "支付寶戶名", "支付宝名", "支付寶名"
        ]) or key in [
            "名", "数字名", "數字名", "数位名", "數位名",
            "数字户名", "數字戶名", "数位户名", "數位戶名",
            "数字人民币户名", "數字人民幣戶名", "數位人民幣戶名", "数位人民币户名"
        ]:
            info["name"] = val

        # 匹配手机号
        elif any(k in key for k in ["手机", "手機", "电话", "電話", "联系方式"]):
            raw_phone = val

        # 匹配商城界面
        elif any(k in key for k in ["商城界面", "商城模板", "界面", "模板"]):
            info["skin"] = val.replace("预设", "")

        # 1. 优先精准匹配数字人民币账号（支持：账号/帳號/帐号/卡号/卡號/钱包/錢包等各种组合）
        elif any(k in key for k in [
            "数字人民币", "數字人民幣", "數位人民幣", "数位人民币",
            "数字账号", "數字帳號", "数字帐号", "數字账号", "数位账号", "數位帳號",
            "数字卡号", "數字卡號", "数位卡号", "數位卡號",
            "数字R人民币", "數字R人民幣", "数字R", "數字R",
            "数币", "數幣", "钱包", "錢包"
        ]) or (info.get("type") == "digital_wallet" and any(k in key for k in ["账号", "帳號", "帐号", "卡号", "卡號"])):
            raw_accounts["digital"] = val

        # 2. 匹配支付宝账号
        elif key in [
            "支付宝", "支付寶", "支付宝账号", "支付寶帳號", "支付宝帐号", "支",
            "支付宝卡号", "支付寶卡號"
        ] or (info.get("type") == "alipay" and any(k in key for k in ["账号", "帳號", "帐号", "卡号", "卡號"])):
            raw_accounts["alipay"] = val

        # 匹配支行
        elif any(k in key for k in ["支行", "分行", "网点", "網點", "开户支行", "開戶支行", "银行支行", "銀行支行"]):
            info["branch_name"] = val

        # 匹配银行名称
        elif any(k in key for k in ["银行名称", "銀行名稱", "开户行", "開戶行", "行名"]) or key in ["银行", "銀行"]:
            if "支行" not in key:
                if "-" in val or " " in val:
                    bank_parts = re.split(r'[- ]+', val, maxsplit=1)
                    info["bank_name"] = bank_parts[0].strip()
                    info["branch_name"] = bank_parts[1].strip()
                else:
                    info["bank_name"] = val

        # 3. 匹配银行卡号
        elif key in ["银行账号", "銀行帳號", "银行卡号", "銀行卡號", "银", "銀"] or (
            info.get("type") == "bank" and any(k in key for k in ["账号", "帳號", "帐号", "卡号", "卡號"])
        ):
            raw_accounts["bank"] = val

        # 4. 保底提取平台主账号
        elif "account" not in info and key in ["账号", "帳號", "帐号", "会员号", "會員號"]:
            info["account"] = val.lower()

    if empty_fields:
        for ef in empty_fields:
            errors.append(f"• 【<b>{html.escape(ef)}</b>】内容为空，请检查是否有漏填或只有空格！")

    if info.get("type") == "digital_wallet" and not raw_accounts.get("digital") and raw_accounts.get("bank"):
        raw_accounts["digital"] = raw_accounts.pop("bank")

    if not info.get("account"):
        errors.append("• 未提取到【平台会员账号】！")

    if raw_phone:
        if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_phone):
            errors.append(f"• 手机号错误: <code>{html.escape(raw_phone)}</code>（只允许数字）")
        else:
            digits_phone = re.sub(r'\D', '', raw_phone)
            if len(digits_phone) < 11:
                errors.append(f"• 手机号位数错误: <code>{html.escape(raw_phone)}</code>（至少11位）")
            else:
                info["phone"] = digits_phone

    info_type = info.get("type")

    if info_type == "digital_wallet":
        raw_val = raw_accounts.get("digital")
        if not raw_val and "卡号" not in empty_fields and "账号" not in empty_fields and "帳號" not in empty_fields:
            errors.append("• 未找到【数字人民币账号】！")
        elif raw_val:
            if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_val):
                errors.append(f"• 数字人民币账号错误: <code>{html.escape(raw_val)}</code>（只允许数字）")
            else:
                digits = re.sub(r'\D', '', raw_val)
                if not digits:
                    errors.append(f"• 数字人民币账号无效: <code>{html.escape(raw_val)}</code>")
                else:
                    info["digital_account"] = digits

    elif info_type == "alipay":
        raw_val = raw_accounts.get("alipay")
        if raw_val:
            if "@" in raw_val:
                email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', raw_val)
                if email_match:
                    info["alipay_account"] = email_match.group(0)
                else:
                    errors.append(f"• 支付宝邮箱格式错误: <code>{html.escape(raw_val)}</code>")
            else:
                if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_val):
                    errors.append(f"• 支付宝账号错误: <code>{html.escape(raw_val)}</code>（仅支持手机号或邮箱）")
                else:
                    digits = re.sub(r'\D', '', raw_val)
                    if not digits:
                        errors.append(f"• 支付宝账号无效: <code>{html.escape(raw_val)}</code>")
                    else:
                        info["alipay_account"] = digits
        else:
            if not info.get("phone") and not errors and "卡号" not in empty_fields:
                errors.append("• 缺失支付宝账号及手机号！")
            elif info.get("phone"):
                info["alipay_account"] = info.get("phone")

    elif info_type == "bank":
        raw_val = raw_accounts.get("bank")
        if not raw_val and "卡号" not in empty_fields and "账号" not in empty_fields and "帳號" not in empty_fields:
            errors.append("• 未找到【银行卡号/账号】！")
        elif raw_val:
            if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_val):
                errors.append(f"• 银行卡号错误: <code>{html.escape(raw_val)}</code>（只允许数字）")
            else:
                digits = re.sub(r'\D', '', raw_val)
                if not digits:
                    errors.append(f"• 银行卡号无效: <code>{html.escape(raw_val)}</code>")
                else:
                    info["bank_account"] = digits

        if not info.get("bank_name") and "银行" not in empty_fields and "銀行" not in empty_fields:
            errors.append("• 缺少【银行名称】！")
        if not info.get("branch_name") and "支行" not in empty_fields:
            errors.append("• 缺少【支行名称】！")

    # 单笔订单号：只有消息包含“单笔”字段时才走单笔商城
    single_order_match = re.search(r'(?im)^[ \t]*(?:单笔|單筆)[ \t]*[:：][ \t]*(.+?)[ \t]*$', clean_text)
    if single_order_match:
        single_order_no = single_order_match.group(1).strip()
        if single_order_no:
            info["single_order_no"] = single_order_no
        else:
            errors.append("• 【单笔】订单号为空！")

    if errors:
        error_summary = "❌ <b>建店失败！检测到以下输入错误：</b>\n\n" + "\n".join(errors)
        return None, error_summary

    return info, ""


# 3. Playwright 自动化建店逻辑
async def create_and_setup_shop(info: dict, task_id: str) -> tuple[str, str]:
    if not BASE_ADMIN_URL:
        raise Exception("未检测到环境变量 ADMIN_URL！")
    if not ADMIN_USER or not ADMIN_PASS:
        raise Exception("未检测到 ADMIN_USER / ADMIN_PASS！")

    base_account = info.get("account")
    suffix_num = 0
    final_account = base_account
    target_skin = info.get("skin", "极速微商")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-blink-features=AutomationControlled']
        )
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            page.set_default_timeout(20000)

            if task_id in ACTIVE_TASKS:
                ACTIVE_TASKS[task_id]["page"] = page

            async def click_and_wait_element(click_locator, wait_locator, timeout=20000):
                await click_locator.click()
                await wait_locator.wait_for(state="visible", timeout=timeout)

            # 1. 登录后台
            await page.goto(BASE_ADMIN_URL, wait_until="domcontentloaded")
            user_input = page.locator(
                "#admin_user_email, #user_email, input[type='email'], input[name*='email'], input[name*='login'], input[name*='username'], input[type='text']"
            ).first

            try:
                await user_input.wait_for(state="visible", timeout=20000)
            except Exception:
                raise Exception(f"无法找到登录框！标题: 【{await page.title()}】，地址: {page.url}")

            await user_input.fill(ADMIN_USER)
            await page.locator("#admin_user_password, #user_password, input[type='password']").first.fill(ADMIN_PASS)
            
            submit_btn = page.locator("input[type='submit'], button[type='submit'], input[name='commit']").first
            await submit_btn.click()
            await page.wait_for_load_state("domcontentloaded")

            async def search_account(acc_name: str):
                await page.goto(f"{BASE_ADMIN_URL}/merchants", wait_until="domcontentloaded")
                search_input = page.locator("input[name*='account'], #search_account, input[type='search'], input[type='text']").first
                await search_input.wait_for(state="visible", timeout=20000)
                await search_input.fill(acc_name)

                search_btn = page.locator("button:has-text('搜尋'), button:has-text('搜索'), input[type='submit'], .btn-primary").first
                if await search_btn.is_visible():
                    await search_btn.click()
                else:
                    await search_input.press("Enter")

                await page.locator("tbody tr").first.wait_for(state="visible", timeout=20000)

            # 2. 尝试递增后缀建店
            while True:
                current_account = base_account if suffix_num == 0 else f"{base_account}{suffix_num:02d}"
                await page.goto(f"{BASE_ADMIN_URL}/merchants/new", wait_until="domcontentloaded")
                merchant_username = page.locator("#merchant_username").first
                await merchant_username.wait_for(state="visible", timeout=20000)

                await merchant_username.fill(current_account)
                if await page.locator("#merchant_password").is_visible():
                    await page.locator("#merchant_password").fill("a12345")
                if await page.locator("#merchant_password_confirmation").is_visible():
                    await page.locator("#merchant_password_confirmation").fill("a12345")

                if await page.locator("#merchant_sprite_platform").is_visible():
                    try:
                        await page.locator("#merchant_sprite_platform").select_option(label="jj")
                    except Exception:
                        await page.locator("#merchant_sprite_platform").select_option(value="jj")

                if await page.locator("#merchant_account_name").is_visible():
                    await page.locator("#merchant_account_name").fill(info.get("name", ""))
                if await page.locator("#merchant_phone").is_visible():
                    await page.locator("#merchant_phone").fill(info.get("phone", ""))

                info_type = info.get("type", "alipay")
                default_num = "6226220809397366"

                bank_name_input = page.locator("#merchant_bank_accounts_attributes_0_bank_name, input[id$='_bank_name']").first
                branch_name_input = page.locator("#merchant_bank_accounts_attributes_0_bank_branch_name, #merchant_bank_accounts_attributes_0_branch_name, input[id$='_bank_branch_name'], input[id$='_branch_name']").first
                card_no_input = page.locator("#merchant_bank_accounts_attributes_0_account_no, input[id$='_account_no']").first

                if info_type == "bank":
                    bank_name = info.get("bank_name", "")
                    branch_name = info.get("branch_name", "")
                    bank_acc = info.get("bank_account", "")

                    if await bank_name_input.is_visible(): await bank_name_input.fill(bank_name)
                    if await branch_name_input.is_visible(): await branch_name_input.fill(branch_name)
                    if await card_no_input.is_visible(): await card_no_input.fill(bank_acc)
                else:
                    if await bank_name_input.is_visible(): await bank_name_input.fill(default_num)
                    if await branch_name_input.is_visible(): await branch_name_input.fill(default_num)
                    if await card_no_input.is_visible(): await card_no_input.fill(default_num)

                alipay_input = page.locator("#merchant_alipay_accounts_attributes_0_account_name")
                if info_type == "alipay" and await alipay_input.is_visible():
                    await alipay_input.fill(info.get("alipay_account", ""))
                elif await alipay_input.is_visible():
                    await alipay_input.fill("")

                ecny_input = page.locator("#merchant_ecny_accounts_attributes_0_account_name")
                if info_type == "digital_wallet" and await ecny_input.is_visible():
                    await ecny_input.fill(info.get("digital_account", ""))
                elif await ecny_input.is_visible():
                    await ecny_input.fill("")

                shop_template = page.locator("#merchant_store_skin_type")
                if await shop_template.is_visible():
                    try:
                        await shop_template.select_option(label=target_skin)
                    except Exception:
                        await shop_template.select_option(index=1)

                await page.locator("input[name='commit'][value='送出']").first.click()
                await page.wait_for_load_state("domcontentloaded")

                is_used = await page.locator("body").evaluate("el => el.innerText.includes('已经被使用') || el.innerText.includes('已經被使用')")
                if is_used:
                    suffix_num += 1
                else:
                    final_account = current_account
                    break

            # 3. 提取店铺 Link
            await search_account(final_account)
            shop_url = (await page.locator("tbody tr").first.locator("td").nth(3).inner_text()).strip()

            async def run_sub_step(step_name, coro):
                try:
                    await coro
                except Exception as sub_e:
                    print(f"⚠️ [{step_name}] 执行失败或超时（不影响建店主体）: {sub_e}")

            # 4. 批量商品
            async def step_items():
                await click_and_wait_element(
                    page.locator("tbody tr").first.locator("a[href$='/items']"),
                    page.locator("a[href*='/items/new'], a:has-text('導入商品')").first
                )
                await click_and_wait_element(
                    page.locator("a[href*='/items/new'], a:has-text('導入商品')").first,
                    page.locator("#count_of_items, input[name='count_of_items']")
                )
                await page.locator("#count_of_items, input[name='count_of_items']").fill("60")
                await page.locator("input[name='commit'], input[value='送出']").click()
                await page.wait_for_load_state("domcontentloaded")

            await run_sub_step("导入商品", step_items())

            # 5. 移除默认填充的银行卡占位符
            if info_type != "bank":
                async def step_remove_placeholder():
                    await search_account(final_account)
                    await page.locator("tbody tr").first.locator("a[href$='/edit']").click()
                    await page.wait_for_load_state("domcontentloaded")
                    
                    bank_section = page.locator(".nested-fields, div:has(#merchant_bank_accounts_attributes_0_account_no)").first
                    remove_btn = bank_section.locator("a.remove_fields, a:has-text('移除')").first
                    
                    if not await remove_btn.is_visible():
                        remove_btn = page.locator("a.remove_fields, a:has-text('移除')").first

                    if await remove_btn.is_visible():
                        await remove_btn.click()
                        await page.locator("input[name='commit'][value='送出']").first.click()
                        await page.wait_for_load_state("domcontentloaded")

                await run_sub_step("移除占位符", step_remove_placeholder())

            # 6. 出货订单
            async def step_deposit():
                await search_account(final_account)
                await click_and_wait_element(
                    page.locator("tbody tr").first.locator("a[href$='/deposits']"),
                    page.locator("a[href$='/deposits/new'], a:has-text('輸入出貨訂單')").first
                )
                await click_and_wait_element(
                    page.locator("a[href$='/deposits/new'], a:has-text('輸入出貨訂單')").first,
                    page.locator("#quantity, input[name='quantity']")
                )
                await page.locator("#quantity, input[name='quantity']").fill("6000")
                await page.locator("input[name='commit'], input[value='送出']").click()
                await page.wait_for_load_state("domcontentloaded")

            await run_sub_step("输入出货订单", step_deposit())

            # 7. 提现订单
            async def step_withdraw():
                await search_account(final_account)
                await click_and_wait_element(
                    page.locator("tbody tr").first.locator("a[href$='/withdraws']"),
                    page.locator("a:has-text('輸入拼多多訂單'), a:has-text('輸入提現訂單'), a[href*='/withdraws/new']").first
                )
                withdraw_btn = page.locator("a:has-text('輸入拼多多訂單'), a:has-text('輸入提現訂單'), a[href*='/withdraws/new']").first
                await withdraw_btn.click()
                
                qty_input = page.locator("#quantity, input[name='quantity']")
                await qty_input.wait_for(state="visible", timeout=20000)
                await qty_input.fill("6000")
                await page.locator("input[name='commit'], input[value='送出']").click()
                await page.wait_for_load_state("domcontentloaded")

            await run_sub_step("输入提现订单", step_withdraw())

            msg_text = (
                "✅ <b>建店完成！</b>\n\n"
                f"店铺网址 : <code>{html.escape(shop_url)}</code>\n"
                f"登入帳號 : <code>{html.escape(final_account)}</code>\n"
                "登入密码 : <code>a12345</code>"
            )
            return msg_text, final_account
        except PlaywrightTimeoutError:
            raise Exception("建店关键流程超时，后台响应较慢，请稍后前往后台核对。")
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# ============================================================
# 单笔商城 + JJ 订单后台
# ============================================================

def _clean_text_value(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def _looks_like_human_name(value: str) -> bool:
    """判断 JJ 实名是否像真人姓名；明显数字/乱码则使用管理员代收。"""
    value = _clean_text_value(value)
    if not value or len(value) > 40:
        return False

    # 任何数字都不当作真人姓名
    if re.search(r"\d", value):
        return False

    # 中文姓名：2~6 个汉字
    if re.fullmatch(r"[\u4e00-\u9fff]{2,6}", value):
        return True

    # 英文姓名：允许空格、连字符、撇号
    if re.fullmatch(r"[A-Za-z][A-Za-z '\-]{1,39}", value):
        letters = re.sub(r"[^A-Za-z]", "", value)
        return len(letters) >= 2

    return False


def _safe_manager_name(value: str) -> str:
    value = _clean_text_value(value)
    return value if _looks_like_human_name(value) else MANAGER_RECEIVE_NAME


def _random_delivery_time(created_time: datetime) -> datetime:
    """建立时间后 1~2 天，随机 08:00~18:00。"""
    days = random.choice([1, 2])
    day = created_time + timedelta(days=days)
    hour = random.randint(8, 17)
    minute = random.randint(0, 59)
    # 18:00 作为边界也允许
    if random.random() < 0.08:
        hour, minute = 18, 0
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _parse_jj_datetime(value: str):
    value = _clean_text_value(value)
    if not value:
        return None
    formats = [
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
        "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


async def _login_generic(page, url, username, password, use_totp=False):
    if not url:
        raise Exception("后台 URL 未配置")
    if not username or not password:
        raise Exception("后台账号或密码未配置")

    await page.goto(url, wait_until="domcontentloaded")
    page.set_default_timeout(20000)

    user_input = page.locator(
        "#admin_user_email, #user_email, input[type='email'], "
        "input[name*='email'], input[name*='login'], input[name*='username'], "
        "input[type='text']"
    ).first
    await user_input.wait_for(state="visible", timeout=20000)
    await user_input.fill(username)

    password_input = page.locator(
        "#admin_user_password, #user_password, input[type='password']"
    ).first
    await password_input.fill(password)

    if use_totp:
        if not JJ_2FA_SECRET:
            raise Exception("未配置 JJ_2FA_SECRET")
        if pyotp is None:
            raise Exception("缺少 pyotp，请在 requirements.txt 加入 pyotp")

        totp_code = pyotp.TOTP(JJ_2FA_SECRET).now()

        # 尽量通过 label/name/placeholder 找 Google 验证码输入框
        totp = page.locator(
            "input[name*='otp'], input[name*='2fa'], input[name*='code'], "
            "input[placeholder*='Google'], input[placeholder*='验证码'], "
            "input[placeholder*='驗證碼'], input[autocomplete='one-time-code']"
        ).first

        if await totp.count() == 0:
            # 兜底：密码框之后的数字/文本输入框
            candidates = page.locator("input:not([type='hidden']):not([type='password'])")
            count = await candidates.count()
            for i in range(count):
                el = candidates.nth(i)
                try:
                    if await el.is_visible():
                        ph = (await el.get_attribute("placeholder") or "").lower()
                        nm = (await el.get_attribute("name") or "").lower()
                        if any(x in (ph + " " + nm) for x in ["otp", "2fa", "code", "google", "驗證", "验证"]):
                            totp = el
                            break
                except Exception:
                    continue

        await totp.wait_for(state="visible", timeout=10000)
        await totp.fill(totp_code)

    submit_btn = page.locator(
        "input[type='submit'], button[type='submit'], input[name='commit']"
    ).first
    await submit_btn.click()
    await page.wait_for_load_state("domcontentloaded")


async def _first_visible(page, selectors, timeout=5000):
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            await loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


async def _fill_by_label(page, labels, value, required=False):
    """按 label/附近文本找输入框；找不到时返回 False。"""
    if value is None:
        value = ""

    for label_text in labels:
        # label 直接关联
        label = page.locator(f"label:has-text('{label_text}')").first
        try:
            if await label.count() and await label.is_visible():
                for attr in ["for"]:
                    target_id = await label.get_attribute(attr)
                    if target_id:
                        target = page.locator(f"#{target_id}").first
                        if await target.count() and await target.is_visible():
                            await target.fill(str(value))
                            return True
                # label 后面的 input/select/textarea
                parent = label.locator("xpath=..")
                target = parent.locator("input, textarea, select").first
                if await target.count() and await target.is_visible():
                    if await target.evaluate("(e)=>e.tagName") == "SELECT":
                        try:
                            await target.select_option(label=str(value))
                        except Exception:
                            await target.select_option(value=str(value))
                    else:
                        await target.fill(str(value))
                    return True
        except Exception:
            pass

    if required:
        raise Exception(f"找不到字段：{' / '.join(labels)}")
    return False


async def _select_any_option(select_loc):
    if not select_loc:
        return False
    try:
        options = await select_loc.locator("option").evaluate_all(
            "(els) => els.map(e => ({value:e.value,text:e.textContent.trim()}))"
        )
        usable = [x for x in options if x.get("value") not in ("", None)]
        if usable:
            await select_loc.select_option(value=usable[0]["value"])
            return True
    except Exception:
        pass
    return False


async def _single_search_account(page, account):
    # 单笔商城实际商户列表是 /market_manager/merchants，搜索框实际为 q_username_eq。
    await page.goto(f"{SINGLE_ADMIN_ROOT}/market_manager/merchants", wait_until="domcontentloaded")

    # 优先使用截图/DevTools 已确认的真实字段，再做旧版兼容。
    search_input = await _first_visible(page, [
        "#q_username_eq",
        "input[name='q[username_eq]']",
        "#q_username",
        "input[name='q[username]']",
        "input[name*='account']",
        "#search_account",
        "input[type='search']",
        "input[type='text']",
    ], timeout=8000)
    if not search_input:
        raise Exception(f"单笔商城找不到商户搜索框；当前地址：{page.url}；标题：{await page.title()}")
    await search_input.fill(account)

    search_btn = await _first_visible(page, [
        "button:has-text('搜尋')",
        "button:has-text('搜索')",
        "input[type='submit']",
        ".btn-primary",
    ], timeout=3000)
    if search_btn:
        await search_btn.click()
    else:
        await search_input.press("Enter")

    await page.wait_for_timeout(500)
    rows = page.locator("tbody tr")
    for i in range(await rows.count()):
        row = rows.nth(i)
        try:
            txt = _clean_text_value(await row.inner_text())
            if account.lower() in txt.lower():
                return row
        except Exception:
            pass
    raise Exception(f"单笔商城找不到商户【{account}】")


async def _create_single_shop(info: dict, task_id: str):
    if not SINGLE_ADMIN_URL:
        raise Exception("未检测到环境变量 SINGLE_ADMIN_URL！")
    if not SINGLE_ADMIN_USER or not SINGLE_ADMIN_PASS:
        raise Exception("未检测到 SINGLE_ADMIN_USER / SINGLE_ADMIN_PASS！")

    base_account = info["account"]
    target_skin = info.get("skin", "极速微商")
    info_type = info.get("type", "alipay")
    suffix_num = 0
    final_account = base_account

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(20000)
            if task_id in ACTIVE_TASKS:
                ACTIVE_TASKS[task_id]["page"] = page

            await _login_generic(page, SINGLE_ADMIN_URL, SINGLE_ADMIN_USER, SINGLE_ADMIN_PASS)

            while True:
                current_account = base_account if suffix_num == 0 else f"{base_account}{suffix_num:02d}"
                await page.goto(f"{SINGLE_ADMIN_ROOT}/market_manager/merchants/new", wait_until="domcontentloaded")
                merchant_username = page.locator("#merchant_username").first
                try:
                    await merchant_username.wait_for(state="visible", timeout=20000)
                except Exception:
                    current_url = page.url
                    title = await page.title()
                    body_preview = ""
                    try:
                        body_preview = (await page.locator("body").inner_text())[:500].replace("\n", " ")
                    except Exception:
                        pass
                    raise Exception(f"单笔商城未进入【新增商户】页面；当前地址：{current_url}；标题：{title}；页面内容：{body_preview}")
                await merchant_username.fill(current_account)

                for sel in ["#merchant_password", "#merchant_password_confirmation"]:
                    loc = page.locator(sel)
                    if await loc.count() and await loc.is_visible():
                        await loc.fill("a12345")

                sprite = page.locator("#merchant_sprite_platform")
                if await sprite.count() and await sprite.is_visible():
                    try:
                        if await sprite.is_enabled():
                            try:
                                await sprite.select_option(label="jj")
                            except Exception:
                                await sprite.select_option(value="jj")
                    except Exception:
                        pass

                for sel, val in [
                    ("#merchant_account_name", info.get("name", "")),
                    ("#merchant_phone", info.get("phone", "")),
                ]:
                    loc = page.locator(sel)
                    if await loc.count() and await loc.is_visible():
                        await loc.fill(val)

                default_num = "6226220809397366"
                bank_name_input = page.locator(
                    "#merchant_bank_accounts_attributes_0_bank_name, input[id$='_bank_name']"
                ).first
                branch_name_input = page.locator(
                    "#merchant_bank_accounts_attributes_0_bank_branch_name, #merchant_bank_accounts_attributes_0_branch_name, input[id$='_bank_branch_name'], input[id$='_branch_name']"
                ).first
                card_no_input = page.locator(
                    "#merchant_bank_accounts_attributes_0_account_no, input[id$='_account_no']"
                ).first

                if info_type == "bank":
                    vals = [
                        (bank_name_input, info.get("bank_name", "")),
                        (branch_name_input, info.get("branch_name", "")),
                        (card_no_input, info.get("bank_account", "")),
                    ]
                else:
                    vals = [
                        (bank_name_input, default_num),
                        (branch_name_input, default_num),
                        (card_no_input, default_num),
                    ]

                for loc, val in vals:
                    try:
                        if await loc.is_visible():
                            await loc.fill(val)
                    except Exception:
                        pass

                alipay_input = page.locator("#merchant_alipay_accounts_attributes_0_account_name").first
                if await alipay_input.count() and await alipay_input.is_visible():
                    await alipay_input.fill(info.get("alipay_account", "") if info_type == "alipay" else "")

                ecny_input = page.locator("#merchant_ecny_accounts_attributes_0_account_name").first
                if await ecny_input.count() and await ecny_input.is_visible():
                    await ecny_input.fill(info.get("digital_account", "") if info_type == "digital_wallet" else "")

                shop_template = page.locator("#merchant_store_skin_type").first
                if await shop_template.count() and await shop_template.is_visible():
                    try:
                        await shop_template.select_option(label=target_skin)
                    except Exception:
                        try:
                            await shop_template.select_option(index=1)
                        except Exception:
                            pass

                await page.locator("input[name='commit'][value='送出']").first.click()
                await page.wait_for_load_state("domcontentloaded")

                body_text = await page.locator("body").inner_text()
                is_used = any(x in body_text for x in ["已经被使用", "已經被使用"])
                if is_used:
                    suffix_num += 1
                    continue

                final_account = current_account
                break

            await _single_search_account(page, final_account)
            shop_url = ""
            try:
                shop_url = (await page.locator("tbody tr").first.locator("td").nth(3).inner_text()).strip()
            except Exception:
                shop_url = ""

            # 商品 60
            await page.locator("tbody tr").first.locator("a[href$='/items']").click()
            await page.wait_for_load_state("domcontentloaded")
            import_btn = page.locator("a[href*='/items/new'], a:has-text('導入商品'), a:has-text('导入商品')").first
            await import_btn.wait_for(state="visible", timeout=20000)
            await import_btn.click()
            await page.locator("#count_of_items, input[name='count_of_items']").fill("60")
            await page.locator("input[name='commit'], input[value='送出']").click()
            await page.wait_for_load_state("domcontentloaded")

            # 非银行付款才移除默认银行占位符
            if info_type != "bank":
                await _single_search_account(page, final_account)
                await page.locator("tbody tr").first.locator("a[href$='/edit']").click()
                await page.wait_for_load_state("domcontentloaded")
                bank_section = page.locator(
                    ".nested-fields, div:has(#merchant_bank_accounts_attributes_0_account_no)"
                ).first
                remove_btn = bank_section.locator(
                    "a.remove_fields, a:has-text('移除'), a:has-text('刪除')"
                ).first
                if not await remove_btn.is_visible():
                    remove_btn = page.locator(
                        "a.remove_fields, a:has-text('移除'), a:has-text('刪除')"
                    ).first
                if await remove_btn.count() and await remove_btn.is_visible():
                    await remove_btn.click()
                    await page.locator("input[name='commit'][value='送出']").first.click()
                    await page.wait_for_load_state("domcontentloaded")

            # 到这里为止，单笔商城建店流程已经完成。
            # JJ 查询与充值由 Worker 在建店成功之后独立执行，避免 JJ 后台异常影响建店。
            msg_text = (
                "✅ <b>单笔商城建店完成！</b>\n\n"
                f"店铺网址 : <code>{html.escape(shop_url)}</code>\n"
                f"登入帐号 : <code>{html.escape(final_account)}</code>\n"
                "登入密码 : <code>a12345</code>"
            )
            return msg_text, final_account

        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def _jj_open_outbound(page):
    # 用户截图显示的菜单是「出货管理」
    candidates = [
        "a:has-text('出货管理')",
        "a:has-text('出貨管理')",
        "a[href*='guest_payment_orders']",
    ]
    for selector in candidates:
        loc = page.locator(selector).first
        try:
            if await loc.is_visible():
                await loc.click()
                await page.wait_for_load_state("domcontentloaded")
                return
        except Exception:
            continue
    # 如果首页已经是出货管理，也继续
    if "guest_payment_orders" not in page.url:
        raise Exception("JJ 后台找不到【出货管理】页面入口")


async def _jj_unlock_search_range(page):
    """解开 JJ 出货管理的日期范围限制。"""
    # 页面截图对应的锁按钮容器。优先点击容器，避免直接点隐藏 icon。
    selectors = [
        ".toggle-order-search-days-btn-placeholder",
        ".toggle-search-days-btn-placeholder",
        ".toggle-order-search-days-btn",
        ".lock-btn",
        "i.fa-lock.lock-btn",
    ]
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            if await loc.count() and await loc.is_visible():
                cls = (await loc.get_attribute("class") or "").lower()
                if "unlock" in cls:
                    return
                await loc.click(force=True)
                await page.wait_for_timeout(300)
                return
        except Exception:
            continue

    # 如果已经是解锁状态就不用处理。
    for selector in [".unlock-btn", "i.fa-unlock", ".fa-unlock"]:
        loc = page.locator(selector).first
        try:
            if await loc.count() and await loc.is_visible():
                return
        except Exception:
            pass


async def _jj_set_one_year_date(page):
    """设置 JJ 建立日期范围为最近一年。兼容不同版本页面的 ID/name。"""
    from datetime import timezone
    tz8 = timezone(timedelta(hours=8))
    now = datetime.now(tz8)
    start = now - timedelta(days=365)

    async def find_input(selectors):
        # 先查主页面，再查 iframe；同时兼容截图中的精确 ID、name 和旧版字段。
        contexts = [page] + list(page.frames)
        for ctx in contexts:
            for selector in selectors:
                try:
                    loc = ctx.locator(selector).first
                    if await loc.count():
                        return loc
                except Exception:
                    continue
        return None

    start_input = await find_input([
        "#q_created_at_gte",
        "input[name='q[created_at_gte]']",
        "input[id*='created_at_gte']",
        "input[name*='created_at_gte']",
    ])
    end_input = await find_input([
        "#q_created_at_lte",
        "input[name='q[created_at_lte]']",
        "input[id*='created_at_lte']",
        "input[name*='created_at_lte']",
    ])

    if start_input is None or end_input is None:
        # 不要因为日期控件的前端版本差异直接让整笔订单失败。
        # 返回 False，由上层继续使用订单号查询；若控件存在则一定设置一年范围。
        return False

    start_value = start.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    end_value = now.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    async def set_value(loc, value):
        await loc.evaluate(
            """(el, value) => {
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.dispatchEvent(new Event('blur', {bubbles:true}));
            }""",
            value,
        )

    await set_value(start_input, start_value)
    await set_value(end_input, end_value)
    await page.wait_for_timeout(300)
    return True


async def _jj_find_order_input(page, kind):
    if kind == "platform":
        selectors = [
            "#q_id_eq",
            "input[name='q[id_eq]']",
            "#q_id",
            "input[name='q[id]']",
            "input[placeholder*='平台订单']",
            "input[placeholder*='平台訂單']",
        ]
        label = "平台订单号"
    else:
        selectors = [
            "#q_merchant_order_id_or_order_trade_id_eq",
            "input[name='q[merchant_order_id_or_order_trade_id_eq]']",
            "#q_merchant_order_id_or_order_trade_id",
            "input[name='q[merchant_order_id_or_order_trade_id]']",
            "input[placeholder*='其他订单']",
            "input[placeholder*='其他訂單']",
        ]
        label = "其他订单号"

    loc = await _first_visible(page, selectors, timeout=5000)
    if loc:
        return loc

    # label 兜底。
    for txt in [label, label.replace("号", "號")]:
        lab = page.locator(f"label:has-text('{txt}')").first
        try:
            if await lab.count():
                target_id = await lab.get_attribute("for")
                if target_id:
                    target = page.locator(f"#{target_id}").first
                    if await target.count():
                        return target
                target = lab.locator("xpath=..//input[1]").first
                if await target.count():
                    return target
        except Exception:
            pass

    raise Exception(f"JJ 找不到【{label}】输入框")


async def _jj_search(page, order_no, kind):
    inp = await _jj_find_order_input(page, kind)
    await inp.fill("")
    await inp.fill(order_no)

    # 搜索按钮必须限制在 guest_payment_order_search 表单内，避免点到页面其他按钮。
    form = page.locator("#guest_payment_order_search").first
    search_btn = None
    if await form.count():
        search_btn = await _first_visible(form, [
            "input[type='submit']",
            "button[type='submit']",
            "input[value*='搜']",
            "button:has-text('搜索')",
            "button:has-text('搜尋')",
        ], timeout=3000)

    try:
        if search_btn:
            await search_btn.click()
        else:
            await inp.press("Enter")
    except Exception:
        await inp.press("Enter")

    # 搜索后等待真正的结果行出现；截图确认结果行 id 为 guest_payment_order_<UUID>。
    try:
        await page.locator(f"tr#guest_payment_order_{order_no}").wait_for(state="attached", timeout=8000)
    except Exception:
        try:
            await page.locator("span.short-uuid[data-origin-uuid]").first.wait_for(state="attached", timeout=5000)
        except Exception:
            await page.wait_for_timeout(1000)



def _normalize_header(text):
    return re.sub(r"\s+", "", text or "").lower()


async def _extract_jj_row(page, order_no=""):
    """从 JJ 搜索结果中找目标订单。

    JJ 实际页面可能把结果渲染成 table、tr、role=row 或普通 div，
    而平台订单号又可能显示为 acd8c604... 的截断文字。
    因此这里不再只依赖 table/tbody tr，而是优先直接寻找订单号文字，
    再向上寻找对应的行容器；最后才使用单结果兜底。
    """
    wanted = re.sub(r"\s+", "", order_no or "").lower()
    if not wanted:
        return [], []

    prefixes = [wanted]
    if len(wanted) >= 12:
        prefixes.append(wanted[:12])
    if len(wanted) >= 8:
        prefixes.append(wanted[:8])

    def norm(v):
        return re.sub(r"\s+", "", v or "").lower()

    async def row_to_data(row):
        """把各种可能的行容器转换成 headers/cells。"""
        try:
            # 标准 table 行
            cells = row.locator("td")
            if await cells.count():
                table = row.locator("xpath=ancestor::table[1]").first
                headers = table.locator("thead th") if await table.count() else page.locator("thead th")
                header_count = await headers.count()
                header_texts = [
                    _normalize_header(await headers.nth(i).inner_text())
                    for i in range(header_count)
                ]
                cell_texts = [
                    _clean_text_value(await cells.nth(i).inner_text())
                    for i in range(await cells.count())
                ]
                if cell_texts:
                    return header_texts, cell_texts

            # role=row 或普通 div 行：优先直接拿可见文字。
            text = _clean_text_value(await row.inner_text())
            if text:
                # 如果内部存在 role=cell，则按 cell 拆分
                role_cells = row.locator("[role='cell']")
                if await role_cells.count():
                    vals = [_clean_text_value(await role_cells.nth(i).inner_text())
                            for i in range(await role_cells.count())]
                    if vals:
                        return [], vals
                # 普通 div 没有明确列时，把整行作为一格，后续用文本兜底解析
                return [], [text]
        except Exception:
            pass
        return [], []

    # 1) JJ 实际页面最可靠的识别方式。
    # 截图已确认：结果行本身的 id 就是 guest_payment_order_<完整UUID>，
    # 且 span.short-uuid 的 data-origin-uuid 也保存完整 UUID。
    # 因此先用“结果行 id”精确定位，再用 data-origin-uuid 双重确认。
    try:
        exact_row = page.locator(f"tr#guest_payment_order_{order_no}").first
        if await exact_row.count():
            h, c = await row_to_data(exact_row)
            if c:
                return h, c
    except Exception:
        pass

    try:
        uuid_nodes = page.locator("span.short-uuid[data-origin-uuid]")
        uuid_count = await uuid_nodes.count()
        for i in range(uuid_count):
            node = uuid_nodes.nth(i)
            try:
                # 不要求 visible；后台表格可能在滚动区域或刚刷新完成时
                # 暂时被 Playwright 判定为不可见，但 DOM 已经存在。
                origin = norm(await node.get_attribute("data-origin-uuid"))
                if origin != wanted:
                    continue

                # 截图确认标准结果行结构：
                # <tr id="guest_payment_order_<uuid>">...<span class="short-uuid" ...>
                tr = node.locator("xpath=ancestor::tr[1]").first
                if await tr.count():
                    h, c = await row_to_data(tr)
                    if c:
                        return h, c

                # 非标准表格时再向上寻找包含 td 的结果容器。
                ancestor = node.locator("xpath=ancestor::*[.//td][1]").first
                if await ancestor.count():
                    h, c = await row_to_data(ancestor)
                    if c:
                        return h, c
            except Exception:
                continue
    except Exception:
        pass

    # 2) 其次才找页面上显示的订单号/截断前缀。
    for prefix in prefixes:
        try:
            # 用 regex 忽略大小写；只要求前缀连续出现，兼容 acd8c604...。
            target = page.get_by_text(re.compile(re.escape(prefix), re.I)).first
            if await target.count() and await target.is_visible():
                # 标准 table
                tr = target.locator("xpath=ancestor::tr[1]").first
                if await tr.count():
                    h, c = await row_to_data(tr)
                    if c:
                        return h, c

                # aria/grid 行
                grid_row = target.locator("xpath=ancestor::*[@role='row'][1]").first
                if await grid_row.count():
                    h, c = await row_to_data(grid_row)
                    if c:
                        return h, c

                # 常见 bootstrap 表格/结果容器：向上找带 td 的祖先
                ancestor = target.locator("xpath=ancestor::*[.//td][1]").first
                if await ancestor.count():
                    h, c = await row_to_data(ancestor)
                    if c:
                        return h, c

                # 最后：目标元素附近的可见父容器
                parent = target.locator("xpath=..").first
                for _ in range(4):
                    if not await parent.count():
                        break
                    h, c = await row_to_data(parent)
                    if c and (len(c) > 1 or prefix in norm(" | ".join(c))):
                        return h, c
                    parent = parent.locator("xpath=..").first
        except Exception:
            continue

    # 3) 标准 table 全量扫描，兼容订单号被放在 title/data-* 属性。
    tables = page.locator("table")
    table_count = await tables.count()
    fallback = None
    for ti in range(table_count):
        table = tables.nth(ti)
        try:
            if not await table.is_visible():
                continue
            rows = table.locator("tbody tr")
            row_count = await rows.count()
            if row_count == 0:
                rows = table.locator("tr")
                row_count = await rows.count()

            headers = table.locator("thead th")
            header_count = await headers.count()
            header_texts = [
                _normalize_header(await headers.nth(i).inner_text())
                for i in range(header_count)
            ]

            for ri in range(row_count):
                row = rows.nth(ri)
                try:
                    if not await row.is_visible():
                        continue
                except Exception:
                    pass
                row_text = _clean_text_value(await row.inner_text())
                nr = norm(row_text)
                if not row_text or any(x in row_text for x in ["没有资料", "沒有資料", "无数据", "無資料"]):
                    continue

                matched = any(x in nr for x in prefixes)
                if not matched:
                    try:
                        attrs = row.locator("[title], [data-original-title], [data-id], [data-value], a")
                        for ai in range(await attrs.count()):
                            el = attrs.nth(ai)
                            for attr in ["title", "data-original-title", "data-id", "data-value", "href"]:
                                val = await el.get_attribute(attr)
                                if val and any(x in norm(val) for x in prefixes):
                                    matched = True
                                    break
                            if matched:
                                break
                    except Exception:
                        pass

                if matched:
                    cells = row.locator("td")
                    cell_texts = [_clean_text_value(await cells.nth(i).inner_text())
                                  for i in range(await cells.count())]
                    return header_texts, cell_texts

                if row_count == 1:
                    cells = row.locator("td")
                    cell_texts = [_clean_text_value(await cells.nth(i).inner_text())
                                  for i in range(await cells.count())]
                    if cell_texts:
                        fallback = (header_texts, cell_texts)
        except Exception:
            continue

    # 4) role=row 全量扫描。
    try:
        role_rows = page.locator("[role='row']")
        rr_count = await role_rows.count()
        for i in range(rr_count):
            row = role_rows.nth(i)
            if not await row.is_visible():
                continue
            text = _clean_text_value(await row.inner_text())
            if any(x in norm(text) for x in prefixes):
                cells = row.locator("[role='cell']")
                if await cells.count():
                    return [], [_clean_text_value(await cells.nth(j).inner_text()) for j in range(await cells.count())]
                if text:
                    return [], [text]
    except Exception:
        pass

    return fallback if fallback else ([], [])


def _cell_by_header(headers, cells, keywords):
    for i, h in enumerate(headers):
        if any(k in h for k in keywords) and i < len(cells):
            return cells[i]
    return ""


async def _query_jj_order(single_order_no, task_id):
    if not JJ_ADMIN_URL:
        raise Exception("未检测到环境变量 JJ_ADMIN_URL！")
    if not JJ_ADMIN_USER or not JJ_ADMIN_PASS:
        raise Exception("未检测到 JJ_ADMIN_USER / JJ_ADMIN_PASS！")
    if not single_order_no:
        raise Exception("单笔订单号为空！")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"]
        )
        try:
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(20000)
            if task_id in ACTIVE_TASKS:
                ACTIVE_TASKS[task_id]["page"] = page

            await _login_generic(page, JJ_ADMIN_URL, JJ_ADMIN_USER, JJ_ADMIN_PASS, use_totp=True)

            # JJ 出货管理页面。JJ_ADMIN_URL 可能是站点根地址，也可能已经包含 /admin。
            jj_base = JJ_ADMIN_URL.rstrip('/')
            if re.search(r'/admin$', jj_base, re.I):
                jj_outbound_url = jj_base + "/guest_payment_orders"
            elif re.search(r'/sign_in$', jj_base, re.I):
                jj_outbound_url = re.sub(r'/sign_in$', '', jj_base, flags=re.I) + "/guest_payment_orders"
            else:
                jj_outbound_url = jj_base + "/admin/guest_payment_orders"
            await page.goto(jj_outbound_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)

            # 先解锁，再把建立日期范围拉回一年。
            await _jj_unlock_search_range(page)
            date_set = await _jj_set_one_year_date(page)

            # 先查平台订单号。
            await _jj_search(page, single_order_no, "platform")
            headers, cells = await _extract_jj_row(page, single_order_no)

            # 如果平台订单号没有结果，再查其他订单号。
            if not cells:
                await _jj_search(page, single_order_no, "other")
                headers, cells = await _extract_jj_row(page, single_order_no)

            if not cells:
                raise Exception(f"JJ 找不到订单：{single_order_no}")

            # JJ 的状态实际位于目标订单这一行最右侧，页面显示类似：
            # 「成功（已補單）」/「成功（已补单）」或「失败（失敗）」。
            # 不能只依赖 thead 的「状态」表头，因为该后台部分版本的表头
            # 并不放在标准 <thead>，导致之前 status_text 为空。
            status_text = _cell_by_header(headers, cells, ["状态", "狀態"])
            full_row = " | ".join(cells)

            # 第一优先：直接读取“精确订单行”的完整 inner_text，再从该行识别状态。
            # 这样不会把页面上方统计卡片的「成功订单数」误认为订单状态。
            row_status_text = ""
            try:
                exact_status_row = page.locator(f"tr#guest_payment_order_{single_order_no}").first
                if await exact_status_row.count():
                    row_status_text = _clean_text_value(await exact_status_row.inner_text())
            except Exception:
                pass

            combined_status_source = row_status_text or status_text or full_row
            # 成功状态允许「成功」「成功（已補單）」「成功（已补单）」等版本。
            # 失败同理；失败订单没有貨運/配送时间是正常情况。
            success_match = re.search(r"成功(?:\s*[（(][^）)]*(?:補單|补单)[^）)]*[）)])?", combined_status_source, re.I)
            failed_match = re.search(r"(?:失败|失敗)(?:\s*[（(][^）)]*[^）)]*[）)])?", combined_status_source, re.I)

            is_failed = bool(failed_match) and not bool(success_match)
            is_success = bool(success_match)

            # 如果精确行没有抓到状态，再扫描当前结果表格中“状态”相关的单元格，
            # 但只接受包含成功/失败字样的单元格，避免被其他字段干扰。
            if not is_success and not is_failed:
                for cell in cells:
                    nc = _clean_text_value(cell)
                    if re.search(r"(?:成功|成功（已補單）|成功（已补单）)", nc, re.I):
                        is_success = True
                        status_text = nc
                        break
                    if re.search(r"(?:失败|失敗)", nc, re.I):
                        is_failed = True
                        status_text = nc
                        break

            if not is_success and not is_failed:
                # 只有在确实无法从目标订单行判断时才报错。
                raise Exception(f"JJ 订单状态无法判断：{combined_status_source[:800]}")

            order_no = _cell_by_header(headers, cells, ["平台订单", "平台訂單", "订单号", "訂單號"])
            recipient_raw = _cell_by_header(headers, cells, ["商户会员", "商戶會員", "实名", "實名", "收件人", "收件人姓名"])
            amount = _cell_by_header(headers, cells, ["交易金额", "交易金額", "金额", "金額"])
            # JJ 后台目前显示为「貨運」；同时兼容未来改成简体「货运」、
            # 「运单号/運單號」等字段名称。失败订单没有貨運是正常状态。
            shipment = _cell_by_header(headers, cells, [
                "运单号", "運單號", "货运", "貨運",
                "物流单号", "物流單號", "货号", "貨號"
            ])
            created = _cell_by_header(headers, cells, ["建立时间", "建立時間", "创建时间", "創建時間", "提交时间", "提交時間"])
            completed = _cell_by_header(headers, cells, ["完成时间", "完成時間"])

            # 没有 header 时，从整行文本中提取金额/日期。
            if not amount:
                for cell in cells:
                    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:CNY|CN¥|元)", cell, re.I)
                    if m:
                        amount = m.group(1)
                        break

            created_dt = _parse_jj_datetime(created) or _parse_jj_datetime(completed)
            if not created_dt:
                # 允许页面使用 ISO/带秒/带时区的时间。
                raw_time = created or completed or ""
                try:
                    created_dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    pass

            if is_success and not shipment:
                # 只有成功订单才尝试从整行找运单号。
                for cell in cells:
                    m = re.search(r"(?:运单号|運單號|物流单号|物流單號|货运|貨運)\s*[:：]?\s*([A-Za-z0-9_-]+)", cell, re.I)
                    if m:
                        shipment = m.group(1)
                        break

            if is_success and not created_dt:
                raise Exception("JJ 成功订单没有读取到建立时间，无法生成配送时间。")
            if not amount:
                raise Exception("JJ 订单没有读取到交易金额。")

            delivery = _random_delivery_time(created_dt) if is_success else None

            return {
                "status": "成功" if is_success else "失败",
                "order_no": order_no or single_order_no,
                "recipient": _safe_manager_name(recipient_raw),
                "amount": amount,
                "shipment": shipment if is_success else "",
                "created": created or completed,
                "created_dt": created_dt,
                "delivery": delivery,
                "raw_headers": headers,
                "raw_cells": cells,
            }
        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def _single_recharge(account, jj_result, task_id=None):
    """重新登录单笔商城并填写充值；不依赖建店时已关闭的浏览器页面。"""
    if not SINGLE_ADMIN_URL:
        raise Exception("未检测到环境变量 SINGLE_ADMIN_URL！")
    if not SINGLE_ADMIN_USER or not SINGLE_ADMIN_PASS:
        raise Exception("未检测到 SINGLE_ADMIN_USER / SINGLE_ADMIN_PASS！")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"]
        )
        try:
            page = await browser.new_page()
            page.set_default_timeout(20000)
            if task_id and task_id in ACTIVE_TASKS:
                ACTIVE_TASKS[task_id]["page"] = page

            await _login_generic(page, SINGLE_ADMIN_URL, SINGLE_ADMIN_USER, SINGLE_ADMIN_PASS)

            # 充值页面截图对应左侧「商户充值管理」。优先从菜单进入，避免猜 URL。
            menu = await _first_visible(page, [
                "a:has-text('商户充值管理')",
                "a:has-text('商戶充值管理')",
            ], timeout=5000)
            if menu:
                await menu.click()
                await page.wait_for_load_state("domcontentloaded")
            else:
                # URL 兜底。
                for url in [
                    f"{SINGLE_ADMIN_ROOT}/deposit_orders",
                    f"{SINGLE_ADMIN_ROOT}/deposits",
                ]:
                    try:
                        await page.goto(url, wait_until="domcontentloaded")
                        if await page.locator("#deposit_order_merchant_id").count():
                            break
                    except Exception:
                        continue

            # 如果菜单打开的是列表页，再找「充值/新增」入口。原生 select2 下拉可能是隐藏的，不能用 is_visible 判断。
            merchant_select = page.locator("#deposit_order_merchant_id").first
            if await merchant_select.count() == 0:
                merchant_select = page.locator("select[name='deposit_order[merchant_id]']").first
            if await merchant_select.count() == 0:
                merchant_select = None
            if not merchant_select:
                add_link = await _first_visible(page, [
                    "a:has-text('新增充值')",
                    "a:has-text('充值')",
                    "a[href*='/deposit_orders/new']",
                    "a[href*='/deposits/new']",
                ], timeout=5000)
                if add_link:
                    await add_link.click()
                    await page.wait_for_load_state("domcontentloaded")
                else:
                    # 最后直接尝试已知 Rails 新增地址。
                    for url in [
                        f"{SINGLE_ADMIN_ROOT}/deposit_orders/new",
                        f"{SINGLE_ADMIN_ROOT}/deposits/new",
                    ]:
                        try:
                            await page.goto(url, wait_until="domcontentloaded")
                            if await page.locator("#deposit_order_merchant_id").count():
                                break
                        except Exception:
                            continue

            merchant_select = page.locator("#deposit_order_merchant_id").first
            if await merchant_select.count() == 0:
                merchant_select = page.locator("select[name='deposit_order[merchant_id]']").first
            if await merchant_select.count() == 0:
                merchant_select = page.locator("select[name*='merchant_id']").first
            if await merchant_select.count() == 0:
                raise Exception("单笔商城充值页面找不到【商户】下拉框。")

            # 商户 select2 的原生 select 是隐藏的，select_option 仍可直接操作。
            matched = False
            options = merchant_select.locator("option")
            for i in range(await options.count()):
                opt = options.nth(i)
                value = await opt.get_attribute("value")
                text = _clean_text_value(await opt.inner_text())
                if value and (text == account or account.lower() == text.lower() or account.lower() in text.lower()):
                    await merchant_select.select_option(value=value)
                    matched = True
                    break
            if not matched:
                try:
                    await merchant_select.select_option(label=account)
                    matched = True
                except Exception:
                    pass
            if not matched:
                raise Exception(f"充值商户下拉找不到刚建立的商户【{account}】。")

            # 触发 select2 / Rails 的 change。
            try:
                await merchant_select.evaluate("el => { el.dispatchEvent(new Event('change', {bubbles:true})); }")
            except Exception:
                pass
            await page.wait_for_timeout(500)

            # 银行账户：截图确认是 disabled，不填。商户选择后后台会自动带入。

            # 收件人资讯 = 任意一个现有选项。截图确认 ID 为 shipment_info_id。
            recipient_info = page.locator("#deposit_order_shipment_info_id").first
            if await recipient_info.count():
                opts = recipient_info.locator("option")
                selected = False
                for i in range(await opts.count()):
                    opt = opts.nth(i)
                    value = await opt.get_attribute("value")
                    disabled = await opt.is_disabled()
                    text = _clean_text_value(await opt.inner_text())
                    if value and not disabled and text not in ("请选择", "請選擇"):
                        await recipient_info.select_option(value=value)
                        selected = True
                        break
                if not selected:
                    raise Exception("充值页面没有可用的【收件人资讯】选项。")
                try:
                    await recipient_info.evaluate("el => el.dispatchEvent(new Event('change', {bubbles:true}))")
                except Exception:
                    pass
            else:
                raise Exception("充值页面找不到【收件人资讯】下拉框。")

            # 买家留言留空。
            comment = page.locator("#deposit_order_buyer_comment").first
            if await comment.count():
                await comment.fill("")

            status = jj_result.get("status")
            if status not in ("成功", "失败"):
                raise Exception("JJ 订单状态无效。")

            # 运单号：只有成功订单填写。
            if status == "成功":
                shipment = jj_result.get("shipment", "")
                if shipment:
                    await page.locator("#deposit_order_shipment_no").first.fill(shipment)

            # 收件人姓名。
            recipient = jj_result.get("recipient") or MANAGER_RECEIVE_NAME
            await page.locator("#deposit_order_recipient_name").first.fill(recipient)

            # 金额。
            amount = re.sub(r"[^0-9.]", "", str(jj_result.get("amount", "")))
            if not amount:
                raise Exception("JJ 订单金额为空。")
            await page.locator("#deposit_order_total_amount").first.fill(amount)

            # 配送时间 = JJ 建立时间后 1~2 天，08:00~18:00；失败订单留空。
            if status == "成功":
                delivery_dt = jj_result.get("delivery")
                if not delivery_dt:
                    raise Exception("成功订单缺少配送时间。")
                delivery_text = delivery_dt.strftime("%Y/%m/%d %H:%M")
                delivery_input = page.locator("#deposit_order_completed_at").first
                if await delivery_input.count():
                    try:
                        await delivery_input.fill(delivery_text)
                    except Exception:
                        await delivery_input.fill(delivery_dt.strftime("%Y-%m-%dT%H:%M"))

            # 建立时间 = JJ 建立时间。截图确认 ID 为 deposit_order_created_at。
            created = jj_result.get("created", "")
            created_input = page.locator("#deposit_order_created_at").first
            if created and await created_input.count():
                try:
                    await created_input.fill(created)
                except Exception:
                    pass

            submit = await _first_visible(page, [
                "input[type='submit'][name='commit'][value='送出']",
                "input[type='submit'][value='送出']",
                "input[name='commit']",
                "button[type='submit']",
            ], timeout=8000)
            if not submit:
                raise Exception("充值页面找不到【送出】按钮。")

            await submit.click()
            await page.wait_for_load_state("domcontentloaded")
            return "已送出"
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# 修改商城界面函数（无排队锁，可并发独立运行）
async def update_shop_skin(account_name: str, new_skin: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-blink-features=AutomationControlled']
        )
        try:
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(20000)

            await page.goto(BASE_ADMIN_URL, wait_until="domcontentloaded")
            user_input = page.locator(
                "#admin_user_email, #user_email, input[type='email'], input[name*='email'], input[name*='login'], input[name*='username'], input[type='text']"
            ).first
            await user_input.fill(ADMIN_USER)
            await page.locator("#admin_user_password, #user_password, input[type='password']").first.fill(ADMIN_PASS)
            await page.locator("input[type='submit'], button[type='submit'], input[name='commit']").first.click()
            await page.wait_for_load_state("domcontentloaded")

            await page.goto(f"{BASE_ADMIN_URL}/merchants", wait_until="domcontentloaded")
            search_input = page.locator("input[name*='account'], #search_account, input[type='search'], input[type='text']").first
            await search_input.fill(account_name)
            search_btn = page.locator("button:has-text('搜尋'), button:has-text('搜索'), input[type='submit'], .btn-primary").first
            if await search_btn.is_visible():
                await search_btn.click()
            else:
                await search_input.press("Enter")
            await page.locator("tbody tr").first.wait_for(state="visible", timeout=20000)

            await page.locator("tbody tr").first.locator("a[href$='/edit']").click()
            await page.wait_for_load_state("domcontentloaded")

            shop_template = page.locator("#merchant_store_skin_type")
            if await shop_template.is_visible():
                try:
                    await shop_template.select_option(label=new_skin)
                except Exception:
                    await shop_template.select_option(label=f"预设{new_skin}")

            await page.locator("input[name='commit'][value='送出']").first.click()
            await page.wait_for_load_state("domcontentloaded")
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# 默认主按钮键盘
def build_main_keyboard(account: str, current_skin: str = "极速微商") -> InlineKeyboardMarkup:
    current_skin = current_skin.replace("预设", "")
    buttons = [
        [InlineKeyboardButton(f"✨ 更改商城界面（当前{current_skin}）", callback_data=f"op:{account}:{current_skin}")]
    ]
    return InlineKeyboardMarkup(buttons)


# 展开风格选项键盘
def build_skin_options_keyboard(account: str, current_skin: str = "极速微商") -> InlineKeyboardMarkup:
    current_skin = current_skin.replace("预设", "")
    buttons = [
        [
            InlineKeyboardButton("极速微商", callback_data=f"sk:jisumeishang:{account}"),
            InlineKeyboardButton("七喵", callback_data=f"sk:qimiao:{account}")
        ],
        [
            InlineKeyboardButton("柒月", callback_data=f"sk:qiyue:{account}"),
            InlineKeyboardButton("音你而来", callback_data=f"sk:yinnierlai:{account}")
        ],
        [
            InlineKeyboardButton("⬅️ 收起", callback_data=f"cl:{account}:{current_skin}")
        ]
    ]
    return InlineKeyboardMarkup(buttons)


# 4. Telegram 消息处理
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    if not user_text:
        return

    chat_type = update.effective_chat.type
    user_id = update.effective_user.id

    if chat_type == "private" and ADMIN_USER_IDS and (user_id not in ADMIN_USER_IDS):
        return

    ignore_keywords = ["店铺网址", "店鋪網址", "登入密碼", "登入密码", "当前界面"]
    if any(k in user_text for k in ignore_keywords):
        return

    trigger_keywords = [
        "账号", "帳號", "帐号", "平台", "平台账号", "平台帳號",
        "数字人民币", "數字人民幣", "數位人民幣", "数位人民币",
        "数字", "數字", "数位", "數位", "支付宝", "支付寶", "银行", "銀行",
        "单笔", "單筆"
    ]
    if not any(k in user_text for k in trigger_keywords):
        return

    parsed_info, error_msg = parse_and_validate_text(user_text)

    if error_msg:
        await update.message.reply_text(error_msg, parse_mode="HTML", disable_web_page_preview=True)
        return

    is_single = bool(parsed_info.get("single_order_no"))

    task_id = f"{update.message.chat_id}_{update.message.message_id}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel:{task_id}")]
    ])

    route_name = "单笔商城" if is_single else "全部商城"
    status_msg = await update.message.reply_text(
        f"⏳ <b>正在自动建店中，请稍候...</b>\n\n流程：{html.escape(route_name)}",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    task = asyncio.create_task(
        run_shop_worker(status_msg, parsed_info, task_id, is_single=is_single)
    )
    ACTIVE_TASKS[task_id] = {
        "task": task,
        "page": None,
        "user_id": user_id
    }



# 建店 Worker 包装（含排队锁控制）
async def run_shop_worker(status_msg, parsed_info, task_id: str, is_single=False):
    try:
        async with BUILD_SHOP_SEMAPHORE:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel:{task_id}")]
            ])
            if is_single:
                await status_msg.edit_text(
                    "⏳ <b>已轮到当前单笔任务，正在自动建店中，请稍候...</b>",
                    reply_markup=keyboard, parse_mode="HTML"
                )
            else:
                await status_msg.edit_text(
                    "⏳ <b>已轮到当前任务，正在自动建店中，请稍候...</b>",
                    reply_markup=keyboard, parse_mode="HTML"
                )

            initial_skin = parsed_info.get("skin", "极速微商").replace("预设", "")

            if is_single:
                # 这里严格只调用已经验证成功的单笔建店流程。
                result_text, final_account = await _create_single_shop(parsed_info, task_id)

                # 建店成功后先明确回报，不让 JJ 查询覆盖/阻塞建店结果。
                await status_msg.edit_text(
                    result_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⏳ 正在查询 JJ 订单...", callback_data="ignore")]
                    ]),
                    parse_mode="HTML", disable_web_page_preview=True
                )

                try:
                    jj_result = await _query_jj_order(parsed_info["single_order_no"], task_id)
                except Exception as jj_error:
                    safe_jj = html.escape(str(jj_error))
                    await status_msg.edit_text(
                        result_text + f"\n\n⚠️ <b>JJ 订单查询失败</b>\n<code>{safe_jj}</code>",
                        reply_markup=build_main_keyboard(final_account, initial_skin),
                        parse_mode="HTML", disable_web_page_preview=True
                    )
                    return

                status_label = jj_result["status"]
                await status_msg.edit_text(
                    result_text + f"\n\n⏳ JJ订单状态：<b>{html.escape(status_label)}</b>，正在自动填写充值...",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⏳ 正在处理充值...", callback_data="ignore")]
                    ]),
                    parse_mode="HTML", disable_web_page_preview=True
                )

                try:
                    recharge_result = await _single_recharge(final_account, jj_result, task_id)
                except Exception as recharge_error:
                    safe_recharge = html.escape(str(recharge_error))
                    await status_msg.edit_text(
                        result_text +
                        f"\n\nJJ订单状态：<b>{html.escape(status_label)}</b>"
                        f"\n❌ <b>新增充值失败</b>\n<code>{safe_recharge}</code>",
                        reply_markup=build_main_keyboard(final_account, initial_skin),
                        parse_mode="HTML", disable_web_page_preview=True
                    )
                    return

                final_text = (
                    result_text + "\n\n"
                    f"JJ订单状态：<b>{html.escape(status_label)}</b>\n"
                    f"充值结果：<b>{html.escape(recharge_result)}</b>"
                )
                await status_msg.edit_text(
                    final_text,
                    reply_markup=build_main_keyboard(final_account, initial_skin),
                    parse_mode="HTML", disable_web_page_preview=True
                )
            else:
                # 全部商城完全沿用原本已经跑通的流程。
                result_text, final_account = await create_and_setup_shop(parsed_info, task_id)
                await status_msg.edit_text(
                    result_text,
                    reply_markup=build_main_keyboard(final_account, initial_skin),
                    parse_mode="HTML", disable_web_page_preview=True
                )

    except asyncio.CancelledError:
        try:
            await status_msg.edit_text("🛑 <b>已取消建店！</b>", parse_mode="HTML")
        except Exception:
            pass
    except Exception as e:
        safe_err = html.escape(str(e))
        try:
            await status_msg.edit_text(
                f"❌ 建店出现错误: {safe_err}",
                parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception:
            pass
    finally:
        ACTIVE_TASKS.pop(task_id, None)


# 5. 回调事件处理
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    click_user_id = query.from_user.id

    # Telegram 的 callback query 必须尽快 answer。若先做网页操作再 answer，
    # 网页操作稍慢时 Telegram 会返回：Query is too old and response timeout expired。
    # 因此所有普通按钮在进入分支前先立即确认一次，后面不再重复 answer。
    if data != "ignore":
        try:
            await query.answer()
        except Exception:
            pass

    if data == "ignore":
        try:
            await query.answer("⏳ 正在修改界面中，请勿重复点击...", show_alert=False)
        except Exception:
            pass
        return

    if data.startswith("cancel:"):
        task_id = data.split(":", 1)[1]

        if task_id not in ACTIVE_TASKS:
            await query.answer("⚠️ 该任务已结束或已被取消。", show_alert=True)
            return

        task_info = ACTIVE_TASKS[task_id]
        origin_user_id = task_info["user_id"]

        if click_user_id != origin_user_id and (ADMIN_USER_IDS and click_user_id not in ADMIN_USER_IDS):
            await query.answer("⚠️ 只有指令发送者或管理员可以取消该任务！", show_alert=True)
            return

        page = task_info.get("page")
        if page and not page.is_closed():
            try:
                await page.close()
            except Exception:
                pass

        task = task_info.get("task")
        if task and not task.done():
            task.cancel()
        await query.answer()

    elif data.startswith("op:"):
        _, account, current_skin = data.split(":", 2)
        keyboard = build_skin_options_keyboard(account, current_skin)
        await query.edit_message_reply_markup(reply_markup=keyboard)

    elif data.startswith("cl:"):
        _, account, current_skin = data.split(":", 2)
        keyboard = build_main_keyboard(account, current_skin)
        await query.edit_message_reply_markup(reply_markup=keyboard)

    elif data.startswith("sk:"):
        _, skin_key, account = data.split(":", 2)
        new_skin_name = SKIN_OPTIONS.get(skin_key, "极速微商")

        # callback query 已在函数开头立即 answer，这里不再重复 answer。

        # 切换按钮为防重复点击状态
        loading_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⏳ 正在切换为【{new_skin_name}】...", callback_data="ignore")]
        ])
        await query.edit_message_reply_markup(reply_markup=loading_keyboard)

        try:
            await update_shop_skin(account, new_skin_name)
            keyboard = build_main_keyboard(account, new_skin_name)
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception as e:
            keyboard = build_skin_options_keyboard(account)
            await query.edit_message_reply_markup(reply_markup=keyboard)


# 6. 主程序入口
def main():
    if not BOT_TOKEN:
        print("❌ 未检测到 BOT_TOKEN 环境变量！")
        sys.exit(1)

    print("🤖 Telegram 机器人服务运行中...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    msg_filter = filters.TEXT & (~filters.COMMAND)

    app.add_handler(MessageHandler(msg_filter, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.run_polling()


if __name__ == "__main__":
    main()
