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
                await page.locator("#merchant_username").wait_for(state="visible", timeout=20000)

                await page.locator("#merchant_username").fill(current_account)
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
                branch_name_input = page.locator("#merchant_bank_accounts_attributes_0_branch_name, input[id$='_branch_name']").first
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
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
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
    await page.goto(f"{SINGLE_ADMIN_ROOT}/merchants", wait_until="domcontentloaded")
    search_input = await _first_visible(page, [
        "input[name*='account']",
        "#search_account",
        "input[type='search']",
        "input[type='text']",
    ])
    if not search_input:
        raise Exception("单笔商城找不到商户搜索框")
    await search_input.fill(account)

    search_btn = page.locator(
        "button:has-text('搜尋'), button:has-text('搜索'), "
        "input[type='submit'], .btn-primary"
    ).first
    try:
        if await search_btn.is_visible():
            await search_btn.click()
        else:
            await search_input.press("Enter")
    except Exception:
        await search_input.press("Enter")

    await page.locator("tbody tr").first.wait_for(state="visible", timeout=20000)


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
                await page.goto(f"{SINGLE_ADMIN_ROOT}/merchants/new", wait_until="domcontentloaded")
                await page.locator("#merchant_username").wait_for(state="visible", timeout=20000)
                await page.locator("#merchant_username").fill(current_account)

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
                                try:
                                    await sprite.select_option(value="jj")
                                except Exception:
                                    pass
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
                    "#merchant_bank_accounts_attributes_0_branch_name, input[id$='_branch_name']"
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

            # JJ 查询
            jj_result = await _query_jj_order(info["single_order_no"], task_id)

            # 回单笔商城充值
            recharge_result = await _single_recharge(
                page=page,
                account=final_account,
                jj_result=jj_result,
            )

            msg_text = (
                "✅ <b>单笔商城流程完成！</b>\n\n"
                f"店铺网址 : <code>{html.escape(shop_url)}</code>\n"
                f"登入帐号 : <code>{html.escape(final_account)}</code>\n"
                "登入密码 : <code>a12345</code>\n"
                f"JJ订单状态 : <b>{html.escape(jj_result['status'])}</b>\n"
                f"充值结果 : <b>{html.escape(recharge_result)}</b>"
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
    """暗锁：初始为锁定，点击后出现解锁图示。"""
    unlock = page.locator("i.fa-unlock.unlock-btn, .fa-unlock.unlock-btn, .unlock-btn").first
    lock = page.locator(".lock-btn, .fa-lock.lock-btn, .fa-lock").first

    # 如果页面上已经存在 unlock-btn，说明已经解锁，不再点击
    try:
        if await unlock.count() and await unlock.is_visible():
            return
    except Exception:
        pass

    # 否则点击用户截图中的锁按钮
    for loc in [lock, page.locator(".toggle-order-search-days-btn-placeholder, .toggle-search-days-btn-placeholder").first]:
        try:
            if await loc.count() and await loc.is_visible():
                await loc.click()
                await page.wait_for_timeout(300)
                return
        except Exception:
            continue

    # 最后尝试点击带 lock 图示的元素
    loc = page.locator("i.fa-lock, i.fa-unlock").first
    if await loc.count() and await loc.is_visible():
        try:
            await loc.click()
            await page.wait_for_timeout(300)
        except Exception:
            pass


async def _jj_set_one_year_date(page):
    """把 JJ 的「建立日期」查询范围固定为最近一年。截图已确认真实 ID。"""
    now = datetime.now()
    start_dt = now - timedelta(days=365)

    start_input = page.locator("#q_created_at_gte").first
    end_input = page.locator("#q_created_at_lte").first

    if await start_input.count() == 0:
        start_input = page.locator("input[name='q[created_at_gte]']").first
    if await end_input.count() == 0:
        end_input = page.locator("input[name='q[created_at_lte]']").first

    if await start_input.count() == 0 or await end_input.count() == 0:
        raise Exception("JJ 找不到建立日期范围输入框（q_created_at_gte / q_created_at_lte）")

    # 页面是 datetime 文本框/日期选择器，直接设置 value 并触发 input/change，
    # 比点击日期选择器逐月回退可靠很多。
    start_value = start_dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    end_value = now.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    async def set_value(loc, value):
        await loc.scroll_into_view_if_needed()
        try:
            await loc.fill(value)
        except Exception:
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

    # 某些版本会把值格式化成带空格的形式，再检查一次；若为空则用备用格式。
    sv = await start_input.input_value()
    ev = await end_input.input_value()
    if not sv or not ev:
        await set_value(start_input, start_dt.strftime("%Y/%m/%d %H:%M"))
        await set_value(end_input, now.strftime("%Y/%m/%d %H:%M"))


async def _jj_find_order_input(page, kind):
    if kind == "platform":
        selectors = [
            "#q_id",
            "input[name='q[id]']",
            "input[name*='platform_order']",
            "input[id*='platform_order']",
            "input[placeholder*='平台订单']",
            "input[placeholder*='平台訂單']",
        ]
    else:
        selectors = [
            "#q_merchant_order_id_or_order_trade_id",
            "input[name='q[merchant_order_id_or_order_trade_id]']",
            "input[name*='merchant_order_id_or_order_trade_id']",
            "input[name*='other_order']",
            "input[id*='other_order']",
            "input[placeholder*='其他订单']",
            "input[placeholder*='其他訂單']",
        ]

    loc = await _first_visible(page, selectors, timeout=3000)
    if loc:
        return loc

    # 根据 label 找输入框
    labels = ["平台订单号", "平台訂單號"] if kind == "platform" else ["其他订单号", "其他訂單號"]
    for txt in labels:
        label = page.locator(f"label:has-text('{txt}')").first
        try:
            if await label.count() and await label.is_visible():
                target_id = await label.get_attribute("for")
                if target_id:
                    loc = page.locator(f"#{target_id}").first
                    if await loc.is_visible():
                        return loc
                target = label.locator("xpath=..").locator("input").first
                if await target.is_visible():
                    return target
        except Exception:
            pass

    raise Exception(f"JJ 找不到【{labels[0]}】输入框")


async def _jj_search(page, order_no, kind):
    inp = await _jj_find_order_input(page, kind)
    await inp.fill(order_no)

    search_btn = page.locator(
        "button:has-text('搜尋'), button:has-text('搜索'), "
        "input[value='搜索'], input[value='搜尋'], .btn-primary"
    ).last
    try:
        await search_btn.click()
    except Exception:
        await inp.press("Enter")

    await page.wait_for_timeout(800)
    # 等表格或「没有资料」类文字出现
    try:
        await page.locator("table tbody tr").first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass


def _normalize_header(text):
    return re.sub(r"\s+", "", text or "").lower()


async def _extract_jj_row(page):
    tables = page.locator("table")
    table_count = await tables.count()
    for ti in range(table_count):
        table = tables.nth(ti)
        try:
            if not await table.is_visible():
                continue
            rows = table.locator("tbody tr")
            if await rows.count() == 0:
                continue
            row = rows.first
            cells = row.locator("td")
            if await cells.count() == 0:
                continue

            headers = table.locator("thead th")
            header_count = await headers.count()
            header_texts = [
                _normalize_header(await headers.nth(i).inner_text())
                for i in range(header_count)
            ]
            cell_texts = [
                _clean_text_value(await cells.nth(i).inner_text())
                for i in range(await cells.count())
            ]
            return header_texts, cell_texts
        except Exception:
            continue
    return [], []


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

            await _login_generic(page, JJ_ADMIN_URL, JJ_ADMIN_USER, JJ_ADMIN_PASS, use_totp=True)
            await _jj_open_outbound(page)
            await _jj_unlock_search_range(page)
            await _jj_set_one_year_date(page)

            # 第一优先：平台订单号
            await _jj_search(page, single_order_no, "platform")
            headers, cells = await _extract_jj_row(page)

            if not cells:
                # 第二优先：其他订单号
                await _jj_search(page, single_order_no, "other")
                headers, cells = await _extract_jj_row(page)

            if not cells:
                raise Exception(f"JJ 找不到订单：{single_order_no}")

            status = _cell_by_header(headers, cells, ["状态", "狀態"])
            status = status or _clean_text_value(await page.locator("table tbody tr").first.inner_text())

            is_success = "成功" in status or "成功" in "".join(cells)
            is_failed = "失败" in status or "失敗" in status or "失败" in "".join(cells)

            # 读取字段：截图明确有交易金额、状态、订单号；货运/实名可能依站点字段名变化
            order_no = _cell_by_header(headers, cells, ["平台订单", "平台訂單", "订单号", "訂單號"])
            recipient = _cell_by_header(headers, cells, ["商户会员", "商戶會員", "实名", "實名", "收件人", "收件人姓名"])
            amount = _cell_by_header(headers, cells, ["交易金额", "交易金額", "金额", "金額"])
            shipment = _cell_by_header(headers, cells, ["货运", "貨運", "运单", "運單", "货号", "貨號"])
            created = _cell_by_header(headers, cells, ["提交时间", "提交時間", "建立时间", "建立時間", "创建时间", "創建時間"])
            completed = _cell_by_header(headers, cells, ["完成时间", "完成時間"])

            # 如果 header mapping 找不到，尝试从整行文本中保留原始内容，避免猜错
            created_dt = _parse_jj_datetime(created)
            if not created_dt:
                created_dt = _parse_jj_datetime(completed)

            return {
                "status": "成功" if is_success and not is_failed else ("失败" if is_failed else "未知"),
                "order_no": order_no,
                "recipient": _safe_manager_name(recipient),
                "amount": amount,
                "shipment": shipment if is_success else "",
                "created": created,
                "created_dt": created_dt,
                "delivery": _random_delivery_time(created_dt) if is_success and created_dt else None,
                "raw_headers": headers,
                "raw_cells": cells,
            }
        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def _single_recharge(page, account, jj_result):
    """单笔商城：进入充值页并按 JJ 订单结果真实提交新增充值。"""
    await _single_search_account(page, account)

    recharge_link = page.locator(
        "a:has-text('充值管理'), a:has-text('商户充值管理'), "
        "a:has-text('商戶充值管理'), a[href*='/deposits/new'], a[href*='/deposit']"
    ).first

    if await recharge_link.count() == 0 or not await recharge_link.is_visible():
        menu = page.locator(
            "a:has-text('商户充值管理'), a:has-text('商戶充值管理'), "
            "a[href*='/deposits/new'], a[href*='/deposit']"
        ).first
        if await menu.count() and await menu.is_visible():
            await menu.click()
        else:
            raise Exception("单笔商城找不到【充值管理】入口")
    else:
        await recharge_link.click()

    await page.wait_for_load_state("domcontentloaded")

    # 1. 商户：真实字段 deposit_order[merchant_id]
    merchant_select = await _first_visible(page, [
        "#deposit_order_merchant_id",
        "select[name='deposit_order[merchant_id]']",
    ], timeout=10000)
    if not merchant_select:
        raise Exception("充值页面找不到【商户】下拉框")

    # 先确认刚建立的账号确实存在于 option 中
    options = await merchant_select.locator("option").evaluate_all(
        "els => els.map(e => ({value:e.value, text:(e.textContent||'').trim()}))"
    )
    matched = next((o for o in options if o["text"] == account), None)
    if not matched:
        matched = next((o for o in options if account in o["text"]), None)
    if not matched:
        raise Exception(f"充值页面找不到刚建立的商户：{account}")

    await merchant_select.select_option(value=matched["value"])
    await page.wait_for_timeout(500)

    # 2. 银行账号：按规则不手填。商户选择后由后台自动带出。
    bank_select = page.locator(
        "#deposit_order_bank_account_id, select[name='deposit_order[bank_account_id]']"
    ).first
    if await bank_select.count():
        try:
            if await bank_select.is_visible() and await bank_select.is_disabled():
                pass
        except Exception:
            pass

    # 3. 收件人资讯：真实字段是 deposit_order[shipment_info_id]
    shipment_info = await _first_visible(page, [
        "#deposit_order_shipment_info_id",
        "select[name='deposit_order[shipment_info_id]']",
    ], timeout=10000)
    if not shipment_info:
        raise Exception("充值页面找不到【收件人资讯】下拉框")

    # 选择第一个真正可用的既有收件人资讯
    usable = await shipment_info.locator("option").evaluate_all(
        "els => els.map(e => ({value:e.value, text:(e.textContent||'').trim(), disabled:e.disabled}))"
    )
    usable = [o for o in usable if o["value"] not in ("", None) and not o["disabled"]]
    if not usable:
        raise Exception("充值页面没有可选择的【收件人资讯】")
    await shipment_info.select_option(value=usable[0]["value"])
    await page.wait_for_timeout(300)

    # 4. 买家留言保持空白
    buyer_comment = page.locator(
        "#deposit_order_buyer_comment, textarea[name='deposit_order[buyer_comment]']"
    ).first
    if await buyer_comment.count():
        try:
            await buyer_comment.fill("")
        except Exception:
            pass

    # 5. 运单号：JJ 成功才填写
    shipment = _clean_text_value(jj_result.get("shipment", ""))
    shipment_no = page.locator(
        "#deposit_order_shipment_no, input[name='deposit_order[shipment_no]']"
    ).first
    if shipment and await shipment_no.count():
        await shipment_no.fill(shipment)

    # 6. 收件人姓名：无有效实名则管理员代收
    recipient = _safe_manager_name(jj_result.get("recipient", ""))
    recipient_name = page.locator(
        "#deposit_order_recipient_name, input[name='deposit_order[recipient_name]']"
    ).first
    if await recipient_name.count():
        await recipient_name.fill(recipient)

    # 7. 金额：JJ 交易金额
    amount_raw = _clean_text_value(jj_result.get("amount", ""))
    amount = re.sub(r"[^0-9.]", "", amount_raw)
    if not amount:
        raise Exception("JJ 订单没有取得有效交易金额，停止提交充值")
    amount_input = page.locator(
        "#deposit_order_total_amount, input[name='deposit_order[total_amount]']"
    ).first
    if not await amount_input.count():
        raise Exception("充值页面找不到【金额】输入框")
    await amount_input.fill(amount)

    # 8. 配送时间：真实字段是 completed_at；成功才填写
    delivery = jj_result.get("delivery")
    if delivery:
        delivery_text = delivery.strftime("%Y/%m/%d %H:%M")
        completed_input = page.locator(
            "#deposit_order_completed_at, input[name='deposit_order[completed_at]']"
        ).first
        if not await completed_input.count():
            raise Exception("充值页面找不到【配送时间】字段 completed_at")
        await completed_input.fill(delivery_text)

    # 9. 建立时间：真实字段 created_at，填 JJ 建立时间
    created = _clean_text_value(jj_result.get("created", ""))
    if created:
        created_input = page.locator(
            "#deposit_order_created_at, input[name='deposit_order[created_at]']"
        ).first
        if await created_input.count():
            await created_input.fill(created)

    # 10. 提交前强制检查，避免出现「显示已送出但后台其实没新增」
    selected_merchant = await merchant_select.input_value()
    selected_shipment_info = await shipment_info.input_value()
    if not selected_merchant:
        raise Exception("提交前检查失败：商户尚未选择")
    if not selected_shipment_info:
        raise Exception("提交前检查失败：收件人资讯尚未选择")
    if not await amount_input.input_value():
        raise Exception("提交前检查失败：金额为空")

    submit = page.locator(
        "input[type='submit'][name='commit'][value='送出']"
    ).first
    if not await submit.count() or not await submit.is_visible():
        raise Exception("找不到单笔商城充值的【送出】按钮")

    await submit.click()
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(800)

    # 后台若有验证错误，必须回报错误，不假报成功
    body = await page.locator("body").inner_text()
    error_loc = page.locator(
        ".field_with_errors, .has-error, .alert-danger, .alert-error, .error, .errors"
    )
    if await error_loc.count():
        try:
            err_text = _clean_text_value(await error_loc.first.inner_text())
            if err_text:
                raise Exception(f"充值提交失败：{err_text[:500]}")
        except Exception as e:
            if str(e).startswith("充值提交失败："):
                raise

    error_words = ["不能为空", "不能為空", "必須", "必须", "無效", "无效", "失败", "失敗"]
    for word in error_words:
        if word in body:
            # 只在页面明显出现错误提示时阻止成功
            if any(k in body for k in ["错误", "錯誤", "失败", "失敗", "不能", "不能为空", "不能為空"]):
                raise Exception(f"充值提交失败：后台返回【{word}】")

    return "已送出"


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
        if BUILD_SHOP_SEMAPHORE.locked():
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel:{task_id}")]
            ])
            await status_msg.edit_text(
                "⏳ <b>前方有建店任务正在处理中，已为您自动加入排队队列，请稍候...</b>",
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        async with BUILD_SHOP_SEMAPHORE:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel:{task_id}")]
            ])
            await status_msg.edit_text(
                "⏳ <b>已轮到当前单笔任务，正在自动建店中，请稍候...</b>" if is_single else "⏳ <b>已轮到当前任务，正在自动建店中，请稍候...</b>",
                reply_markup=keyboard,
                parse_mode="HTML"
            )

            initial_skin = parsed_info.get("skin", "极速微商").replace("预设", "")

            if is_single:
                result_text, final_account = await _create_single_shop(parsed_info, task_id)
            else:
                result_text, final_account = await create_and_setup_shop(parsed_info, task_id)

            keyboard = build_main_keyboard(final_account, initial_skin)
            await status_msg.edit_text(
                result_text,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=True
            )

    except asyncio.CancelledError:
        await status_msg.edit_text("🛑 <b>已取消建店！</b>", parse_mode="HTML")
    except Exception as e:
        safe_err = html.escape(str(e))
        await status_msg.edit_text(
            f"❌ 建店出现错误: {safe_err}",
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    finally:
        ACTIVE_TASKS.pop(task_id, None)



# 5. 回调事件处理
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    click_user_id = query.from_user.id

    if data == "ignore":
        await query.answer("⏳ 正在修改界面中，请勿重复点击...", show_alert=False)
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
        await query.answer()

    elif data.startswith("cl:"):
        _, account, current_skin = data.split(":", 2)
        keyboard = build_main_keyboard(account, current_skin)
        await query.edit_message_reply_markup(reply_markup=keyboard)
        await query.answer()

    elif data.startswith("sk:"):
        _, skin_key, account = data.split(":", 2)
        new_skin_name = SKIN_OPTIONS.get(skin_key, "极速微商")

        await query.answer(f"⏳ 正在切换界面为【{new_skin_name}】...", show_alert=False)

        # 切换按钮为防重复点击状态
        loading_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⏳ 正在切换为【{new_skin_name}】...", callback_data="ignore")]
        ])
        await query.edit_message_reply_markup(reply_markup=loading_keyboard)

        try:
            await update_shop_skin(account, new_skin_name)
            keyboard = build_main_keyboard(account, new_skin_name)
            await query.edit_message_reply_markup(reply_markup=keyboard)
            await query.answer(f"✅ 界面已成功更改为: {new_skin_name}", show_alert=True)
        except Exception as e:
            keyboard = build_skin_options_keyboard(account)
            await query.edit_message_reply_markup(reply_markup=keyboard)
            await query.answer(f"❌ 修改界面失败: {str(e)}", show_alert=True)


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
