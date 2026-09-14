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

# Helper: 保留完整的传入 URL，仅做首尾空格清理与协议补全
def _get_clean_domain(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    match = re.search(r'https?://[^\s]+', url)
    if match:
        url = match.group(0)
    elif not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    return url.rstrip("/")

# 1. 环境变量配置解析
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

admin_id_env = os.environ.get("ADMIN_USER_ID", "").strip()
ADMIN_USER_IDS = set(
    int(x) for x in re.split(r'[,;\s]+', admin_id_env) if x.isdigit()
)

ADMIN_USER = os.environ.get("ADMIN_USER", "").strip()
ADMIN_PASS = os.environ.get("ADMIN_PASS", "").strip()

raw_admin_url = os.environ.get("ADMIN_URL", "").strip()
BASE_ADMIN_URL = _get_clean_domain(raw_admin_url)

# 单笔商城 JJ 订单后台配置
SINGLE_ADMIN_USER = os.environ.get("SINGLE_ADMIN_USER", "").strip()
SINGLE_ADMIN_PASS = os.environ.get("SINGLE_ADMIN_PASS", "").strip()

raw_single_admin_url = os.environ.get("SINGLE_ADMIN_URL", "").strip()
SINGLE_ADMIN_URL = _get_clean_domain(raw_single_admin_url)

JJ_ADMIN_USER = os.environ.get("JJ_ADMIN_USER", "").strip()
JJ_ADMIN_PASS = os.environ.get("JJ_ADMIN_PASS", "").strip()
JJ_2FA_SECRET = os.environ.get("JJ_2FA_SECRET", "").strip()

raw_jj_admin_url = os.environ.get("JJ_ADMIN_URL", "").strip()
JJ_ADMIN_URL = _get_clean_domain(raw_jj_admin_url)

MANAGER_RECEIVE_NAME = "管理员代收"

# 全局任务字典
ACTIVE_TASKS = {}

# 排队锁：同时只允许 1 个建店任务在后台运行
BUILD_SHOP_SEMAPHORE = asyncio.Semaphore(1)

# 商城界面选项
SKIN_OPTIONS = {
    "jisumeishang": "极速微商",
    "qimiao": "七喵",
    "qiyue": "柒月",
    "yinnierlai": "音你而来"
}


# 2. 文本解析与格式校验
def parse_and_validate_text(text: str) -> tuple[dict, str]:
    info = {}
    errors = []

    digital_keywords = [
        "数字R人民币", "數字R人民幣", "数字R", "數字R",
        "数字人民币", "數字人民幣", "數位人民幣", "数位人民币",
        "数字名", "數字名", "数位名", "數位名", "数字户名", "數字戶名",
        "数币", "數幣", "数字", "數字", "数位", "數位",
        "钱包", "錢包", "ecny"
    ]
    bank_keywords = ["银行", "銀行", "开户行", "開戶行", "支行"]
    alipay_keywords = ["支付宝", "支付寶", "支付宝户名", "支付寶戶名", "支付宝名", "支付寶名"]

    if any(k in text for k in alipay_keywords):
        info["type"] = "alipay"
    elif any(k in text for k in digital_keywords):
        info["type"] = "digital_wallet"
    elif any(k in text for k in bank_keywords):
        info["type"] = "bank"
    else:
        info["type"] = "alipay"

    clean_text = re.sub(r'mailto', '', text, flags=re.IGNORECASE)
    clean_text = re.sub(r'https?[^\s]+', '', clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fa5\s：:_\-\.@]+', '', clean_text)

    raw_accounts = {}
    raw_phone = None
    empty_fields = []

    base_ignore_keys = ["余额", "餘額", "状态", "狀態", "备注", "備註", "限制", "风控", "風控", "交易日"]

    lines = clean_text.splitlines()

    for line in lines:
        line = line.strip()
        if not line or any(ik in line for ik in base_ignore_keys):
            continue
        parts = re.split(r'[：:]', line, maxsplit=1)
        if len(parts) < 2:
            continue
        key = re.sub(r'\s+', '', parts[0])
        val = parts[1].strip()
        val = re.sub(r'^[（\(‘“"\'\s]+|[）\)’”"\'\s]+$', '', val)

        if "登入" not in key and (
            any(k in key for k in ["平台", "会员", "會員"])
            or key in ["平台账号", "平台帳號", "平台会员账号", "平台會員帳號", "会员账号", "會員帳號"]
        ):
            if not any(k in key for k in ["支付宝", "支付寶", "银行", "銀行", "数字", "數字", "数位", "數位", "钱包", "錢包"]):
                if val:
                    info["account"] = val.lower()
                    break

    for line in lines:
        line = line.strip()
        if not line:
            continue

        has_base_ignore = any(ik in line for ik in base_ignore_keys)
        has_order_and_last = ("订单" in line or "訂單" in line) and ("最后" in line or "最後" in line)
        if has_base_ignore or has_order_and_last:
            continue

        parts = re.split(r'[：:]', line, maxsplit=1)
        if len(parts) < 2:
            continue

        key = re.sub(r'\s+', '', parts[0])
        val = parts[1].strip()
        val = re.sub(r'^[（\(‘“"\'\s]+|[）\)’”"\'\s]+$', '', val)

        if any(ik in key for ik in base_ignore_keys):
            continue

        if not val:
            if not any(ik in key for ik in ["商城", "模板", "界面"]):
                empty_fields.append(parts[0].strip())
            continue

        if any(k in key for k in [
            "户名", "戶名", "姓名", "名字", "客户姓名", "客戶姓名",
            "支付宝户名", "支付寶戶名", "支付宝名", "支付寶名"
        ]) or key in [
            "名", "数字名", "數字名", "数位名", "數位名",
            "数字户名", "數字戶名", "数位户名", "數位戶名",
            "数字人民币户名", "數字人民幣戶名", "數位人民幣戶名", "数位人民币户名"
        ]:
            info["name"] = val

        elif any(k in key for k in ["手机", "手機", "电话", "電話", "联系方式"]):
            raw_phone = val

        elif any(k in key for k in ["商城界面", "商城模板", "界面", "模板"]):
            info["skin"] = val.replace("预设", "")

        elif any(k in key for k in [
            "数字人民币", "數字人民幣", "數位人民幣", "数位人民币",
            "数字账号", "數字帳號", "数字帐号", "數字账号", "数位账号", "數位帳號",
            "数字卡号", "數字卡號", "数位卡号", "數位卡號",
            "数字R人民币", "數字R人民幣", "数字R", "數字R",
            "数币", "數幣", "钱包", "錢包"
        ]) or (info.get("type") == "digital_wallet" and any(k in key for k in ["账号", "帳號", "帐号", "卡号", "卡號"])):
            raw_accounts["digital"] = val

        elif key in [
            "支付宝", "支付寶", "支付宝账号", "支付寶帳號", "支付宝帐号", "支",
            "支付宝卡号", "支付寶卡號"
        ] or (info.get("type") == "alipay" and any(k in key for k in ["账号", "帳號", "帐号", "卡号", "卡號"])):
            raw_accounts["alipay"] = val

        elif any(k in key for k in ["支行", "分行", "网点", "網點", "开户支行", "開戶支行", "银行支行", "銀行支行"]):
            info["branch_name"] = val

        elif any(k in key for k in ["银行名称", "銀行名稱", "开户行", "開戶行", "行名"]) or key in ["银行", "銀行"]:
            if "支行" not in key:
                if "-" in val or " " in val:
                    bank_parts = re.split(r'[- ]+', val, maxsplit=1)
                    info["bank_name"] = bank_parts[0].strip()
                    info["branch_name"] = bank_parts[1].strip()
                else:
                    info["bank_name"] = val

        elif key in ["银行账号", "銀行帳號", "银行卡号", "銀行卡號", "银", "銀"] or (
            info.get("type") == "bank" and any(k in key for k in ["账号", "帳號", "帐号", "卡号", "卡號"])
        ):
            raw_accounts["bank"] = val

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
            errors.append(f"• 手机号错误 <code>{html.escape(raw_phone)}</code>（只允许数字）")
        else:
            digits_phone = re.sub(r'\D', '', raw_phone)
            if len(digits_phone) < 11:
                errors.append(f"• 手机号位数错误 <code>{html.escape(raw_phone)}</code>（至少11位）")
            else:
                info["phone"] = digits_phone

    info_type = info.get("type")

    if info_type == "digital_wallet":
        raw_val = raw_accounts.get("digital")
        if not raw_val and "卡号" not in empty_fields and "账号" not in empty_fields and "帳號" not in empty_fields:
            errors.append("• 未找到【数字人民币账号】！")
        elif raw_val:
            if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_val):
                errors.append(f"• 数字人民币账号错误 <code>{html.escape(raw_val)}</code>（只允许数字）")
            else:
                digits = re.sub(r'\D', '', raw_val)
                if not digits:
                    errors.append(f"• 数字人民币账号无效 <code>{html.escape(raw_val)}</code>")
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
                    errors.append(f"• 支付宝邮箱格式错误 <code>{html.escape(raw_val)}</code>")
            else:
                if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_val):
                    errors.append(f"• 支付宝账号错误 <code>{html.escape(raw_val)}</code>（仅支持手机号或邮箱）")
                else:
                    digits = re.sub(r'\D', '', raw_val)
                    if not digits:
                        errors.append(f"• 支付宝账号无效 <code>{html.escape(raw_val)}</code>")
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
            errors.append("• 未找到【银行卡号账号】！")
        elif raw_val:
            if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_val):
                errors.append(f"• 银行卡号错误 <code>{html.escape(raw_val)}</code>（只允许数字）")
            else:
                digits = re.sub(r'\D', '', raw_val)
                if not digits:
                    errors.append(f"• 银行卡号无效 <code>{html.escape(raw_val)}</code>")
                else:
                    info["bank_account"] = digits

        if not info.get("bank_name") and "银行" not in empty_fields and "銀行" not in empty_fields:
            errors.append("• 缺少【银行名称】！")
        if not info.get("branch_name") and "支行" not in empty_fields:
            errors.append("• 缺少【支行名称】！")

    single_order_match = re.search(r'(?im)^[\t ]*(?:单笔|單筆)[\t ]*[：:][\t ]*(.+)[\t ]*$', clean_text)
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


# 3. Playwright 自动化建店逻辑 (全部商城)
async def create_and_setup_shop(info: dict, task_id: str) -> tuple[str, str]:
    if not BASE_ADMIN_URL:
        raise Exception("未检测到环境变量 ADMIN_URL！")
    if not ADMIN_USER or not ADMIN_PASS:
        raise Exception("未检测到 ADMIN_USER 或 ADMIN_PASS！")

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
                "input[name='market_manager[username]'], #admin_user_email, #user_email, input[type='email'], input[name='email'], input[name='login'], input[name='username'], input[type='text']"
            ).first

            try:
                await user_input.wait_for(state="visible", timeout=20000)
            except Exception:
                raise Exception(f"无法找到登录框！标题 【{await page.title()}】，地址 {page.url}")

            await user_input.fill(ADMIN_USER)
            await page.locator("#admin_user_password, #user_password, input[type='password']").first.fill(ADMIN_PASS)

            submit_btn = page.locator("input[type='submit'], button[type='submit'], input[name='commit']").first
            await submit_btn.click()
            await page.wait_for_load_state("domcontentloaded")

            domain_root = "/".join(BASE_ADMIN_URL.split("/")[:3])

            async def search_account(acc_name: str):
                await page.goto(f"{domain_root}/merchants", wait_until="domcontentloaded")
                search_input = page.locator("input[name='account'], #search_account, input[type='search'], input[type='text']").first
                await search_input.wait_for(state="visible", timeout=20000)
                await search_input.fill(acc_name)

                search_btn = page.locator("button:has-text('搜尋'), button:has-text('搜索'), input[type='submit'], .btn-primary").first
                if await search_btn.is_visible():
                    await search_btn.click()
                else:
                    await search_input.press("Enter")

                await page.locator("tbody tr").first.wait_for(state="visible", timeout=20000)

            # 2. 递增后缀建店
            while True:
                current_account = base_account if suffix_num == 0 else f"{base_account}{suffix_num:02d}"
                await page.goto(f"{domain_root}/merchants/new", wait_until="domcontentloaded")
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
                    print(f"⚠️ [{step_name}] 执行失败或超时: {sub_e}")

            # 4. 批量商品
            async def step_items():
                await click_and_wait_element(
                    page.locator("tbody tr").first.locator("a[href$='items']"),
                    page.locator("a[href='items/new'], a:has-text('導入商品')").first
                )
                await click_and_wait_element(
                    page.locator("a[href='items/new'], a:has-text('導入商品')").first,
                    page.locator("#count_of_items, input[name='count_of_items']")
                )
                await page.locator("#count_of_items, input[name='count_of_items']").fill("60")
                await page.locator("input[name='commit'], input[value='送出']").click()
                await page.wait_for_load_state("domcontentloaded")

            await run_sub_step("导入商品", step_items())

            # 5. 移除默认占位符
            if info_type != "bank":
                async def step_remove_placeholder():
                    await search_account(final_account)
                    await page.locator("tbody tr").first.locator("a[href$='edit']").click()
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
                    page.locator("tbody tr").first.locator("a[href$='deposits']"),
                    page.locator("a[href$='deposits/new'], a:has-text('輸入出貨訂單')").first
                )
                await click_and_wait_element(
                    page.locator("a[href$='deposits/new'], a:has-text('輸入出貨訂單')").first,
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
                    page.locator("tbody tr").first.locator("a[href$='withdraws']"),
                    page.locator("a:has-text('輸入拼多多訂單'), a:has-text('輸入提現訂單'), a[href='withdraws/new']").first
                )
                withdraw_btn = page.locator("a:has-text('輸入拼多多訂單'), a:has-text('輸入提現訂單'), a[href='withdraws/new']").first
                await withdraw_btn.click()

                qty_input = page.locator("#quantity, input[name='quantity']")
                await qty_input.wait_for(state="visible", timeout=20000)
                await qty_input.fill("6000")
                await page.locator("input[name='commit'], input[value='送出']").click()
                await page.wait_for_load_state("domcontentloaded")

            await run_sub_step("输入提现订单", step_withdraw())

            msg_text = (
                "✅ <b>建店完成！</b>\n\n"
                f"店铺网址： <code>{html.escape(shop_url)}</code>\n"
                f"登入帳號： <code>{html.escape(final_account)}</code>\n"
                "登入密码： <code>a12345</code>"
            )
            return msg_text, final_account
        except PlaywrightTimeoutError:
            raise Exception("建店关键流程超时，后台响应较慢，请稍后前往后台核对。")
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# 单笔商城辅助函数
def _clean_text_value(value):
    return re.sub(r'\s+', ' ', (value or "").strip())


def _looks_like_human_name(value: str) -> bool:
    value = _clean_text_value(value)
    if not value or len(value) > 40:
        return False
    if re.search(r'\d', value):
        return False
    if re.fullmatch(r'[\u4e00-\u9fff]{2,6}', value):
        return True
    if re.fullmatch(r"[A-Za-z][A-Za-z '-]{1,39}", value):
        letters = re.sub(r'[^A-Za-z]', '', value)
        return len(letters) >= 2
    return False


def _safe_manager_name(value: str) -> str:
    value = _clean_text_value(value)
    return value if _looks_like_human_name(value) else MANAGER_RECEIVE_NAME


def _random_delivery_time(created_time: datetime) -> datetime:
    days = random.choice([1, 2])
    day = created_time + timedelta(days=days)
    hour = random.randint(8, 17)
    minute = random.randint(0, 59)
    if random.random() < 0.08:
        hour, minute = 18, 0
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _parse_jj_datetime(value: str):
    value = _clean_text_value(value)
    if not value:
        return None
    formats = [
        "%Y%m%d %H%M%S", "%Y%m%d %H%M",
        "%Y-%m-%d %H%M%S", "%Y-%m-%d %H%M",
        "%Y.%m.%d %H%M%S", "%Y.%m.%d %H%M",
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

    page.set_default_timeout(20000)
    page.set_default_navigation_timeout(30000)
    await page.goto(url, wait_until="commit", timeout=30000)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass

    user_input = page.locator(
        "input[name='market_manager[username]'], "
        "input[name='jj_manager[username]'], "
        "#admin_user_email, #user_email, input[type='email'], "
        "input[name='email'], input[name='login'], input[name='username'], "
        "input[placeholder='帐号'], input[placeholder='账号'], input[type='text']"
    ).first
    await user_input.wait_for(state="visible", timeout=20000)
    await user_input.fill(username)

    password_input = page.locator(
        "input[name='market_manager[password]'], "
        "input[name='jj_manager[password]'], "
        "#admin_user_password, #user_password, input[type='password']"
    ).first
    await password_input.fill(password)

    if use_totp:
        if not JJ_2FA_SECRET:
            raise Exception("未配置 JJ_2FA_SECRET")
        if pyotp is None:
            raise Exception("缺少 pyotp，请在 requirements.txt 加入 pyotp")

        totp_code = pyotp.TOTP(JJ_2FA_SECRET).now()
        totp = page.locator(
            "input[name='otp'], input[name='2fa'], input[name='code'], "
            "input[placeholder='Google'], input[placeholder='验证码'], "
            "input[placeholder='驗證碼'], input[autocomplete='one-time-code']"
        ).first

        if await totp.count() == 0:
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
        "button[type='submit'], input[type='submit'], input[name='commit']"
    ).first
    await submit_btn.wait_for(state="visible", timeout=10000)
    await submit_btn.click()
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    if "sign_in" in page.url.lower() or "login" in page.url.lower():
        # 登录失败时立即报错，不继续等待建店页。
        body = ""
        try:
            body = (await page.locator("body").inner_text())[:500]
        except Exception:
            pass
        raise Exception(f"后台登录没有成功，当前 URL：{page.url}；页面文字：{body}")


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
    if value is None:
        value = ""

    for label_text in labels:
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
                parent = label.locator("xpath=..")
                target = parent.locator("input, textarea, select").first
                if await target.count() and await target.is_visible():
                    if await target.evaluate("(e) => e.tagName") == "SELECT":
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
            "(els) => els.map(e => ({value: e.value, text: e.textContent.trim()}))"
        )
        usable = [x for x in options if x.get("value") not in ("", None)]
        if usable:
            await select_loc.select_option(value=usable[0]["value"])
            return True
    except Exception:
        pass
    return False


async def _single_search_account(page, account):
    domain_root = "/".join(SINGLE_ADMIN_URL.split("/")[:3])
    # 修正单数 market_manager 路径
    await page.goto(f"{domain_root}/market_manager/merchants", wait_until="domcontentloaded")
    search_input = await _first_visible(page, [
        "input[name='account']",
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


# 适应单笔商城 /market_manager 路由结构的建店函数
async def _create_single_shop(info: dict, task_id: str):
    """单笔商城建店。
    登录地址与建店地址是两个不同路由：
      登录: /market_managers/sign_in
      建店: /market_manager/merchants/new
    """
    if not SINGLE_ADMIN_URL:
        raise Exception("未检测到环境变量 SINGLE_ADMIN_URL！")
    if not SINGLE_ADMIN_USER or not SINGLE_ADMIN_PASS:
        raise Exception("未检测到 SINGLE_ADMIN_USER / SINGLE_ADMIN_PASS！")

    base_account = info["account"]
    target_skin = info.get("skin", "极速微商")
    info_type = info.get("type", "alipay")
    final_account = base_account
    domain_root = "/".join(SINGLE_ADMIN_URL.split("/")[:3])
    login_target = SINGLE_ADMIN_URL
    create_url = f"{domain_root}/market_manager/merchants/new"

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
            page.set_default_navigation_timeout(30000)

            if task_id in ACTIVE_TASKS:
                ACTIVE_TASKS[task_id]["page"] = page

            # 1. 登录
            await _login_generic(
                page, login_target, SINGLE_ADMIN_USER, SINGLE_ADMIN_PASS
            )

            # 登录后直接访问实际存在的建店地址。
            response = await page.goto(
                create_url,
                wait_until="commit",
                timeout=30000,
            )
            await page.wait_for_timeout(500)

            status_code = response.status if response else None
            if status_code == 404:
                raise Exception(f"单笔商城建店地址不存在(404)：{create_url}")

            # 如果被踢回登录页，明确报错，不让任务一直等。
            if "sign_in" in page.url.lower() or "login" in page.url.lower():
                raise Exception(f"登录状态失效，当前页面：{page.url}")

            # 2. 建店。帐号重复时持续递增，直到真正创建成功。
            # 不设置 20 次上限：只要后台返回“帐号已存在”，就继续尝试下一个递增帐号。
            suffix_num = 0
            while True:
                current_account = (
                    base_account
                    if suffix_num == 0
                    else f"{base_account}{suffix_num:02d}"
                )

                # 每次尝试都重新打开建店页，避免提交失败后停留在旧页面。
                if suffix_num > 0:
                    await page.goto(
                        create_url,
                        wait_until="commit",
                        timeout=30000,
                    )
                    await page.wait_for_timeout(300)

                # 截图已确认的实际字段 ID 优先使用，其他 selector 只作为备用。
                username_input = page.locator(
                    "#merchant_username, "
                    "input[name='merchant[username]'], "
                    "input[name='market_manager[username]'], "
                    "input[name*='username'], "
                    "input[name*='account']"
                ).first

                try:
                    await username_input.wait_for(
                        state="visible",
                        timeout=15000,
                    )
                except Exception:
                    body = ""
                    try:
                        body = (await page.locator("body").inner_text())[:800]
                    except Exception:
                        pass
                    raise Exception(
                        "无法定位建店【帐号】输入框！"
                        f" 当前 URL：{page.url}"
                        f"；HTTP：{status_code}"
                        f"；页面文字：{body}"
                    )

                await username_input.fill(current_account)

                # 密码与确认密码
                password_input = page.locator(
                    "#merchant_password, input[name='merchant[password]'], input[name='market_manager[password]']"
                ).first
                confirm_input = page.locator(
                    "#merchant_password_confirmation, input[name='merchant[password_confirmation]']"
                ).first

                if await password_input.count() and await password_input.is_visible():
                    await password_input.fill("a12345")
                if await confirm_input.count() and await confirm_input.is_visible():
                    await confirm_input.fill("a12345")

                # Sprite 平台：截图显示该下拉框是 disabled，通常后台已固定为 jj。
                # disabled 的 select 不能执行 select_option，会直接触发 Timeout。
                # 因此只有在“存在、可见、可用”时才尝试选择；如果 disabled，直接保留后台默认值。
                sprite = page.locator("#merchant_sprite_platform").first
                if await sprite.count() and await sprite.is_visible():
                    try:
                        if await sprite.is_enabled():
                            try:
                                await sprite.select_option(label="jj", timeout=5000)
                            except Exception:
                                await sprite.select_option(value="jj", timeout=5000)
                        else:
                            print("ℹ️ merchant_sprite_platform 当前为 disabled，保留后台默认平台。")
                    except Exception as e:
                        # 这个字段不是单笔建店的阻塞条件；如果后台把它锁死，继续建店。
                        print(f"⚠️ Sprite 平台选择跳过：{e}")

                # 户名、电话
                account_name_input = page.locator("#merchant_account_name").first
                phone_input = page.locator("#merchant_phone").first
                if await account_name_input.count() and await account_name_input.is_visible():
                    await account_name_input.fill(info.get("name", ""))
                if await phone_input.count() and await phone_input.is_visible():
                    await phone_input.fill(info.get("phone", ""))

                # 银行帐户。这里使用已确认的 bank_branch_name ID。
                default_num = "6226220809397366"
                bank_name_input = page.locator(
                    "#merchant_bank_accounts_attributes_0_bank_name"
                ).first
                branch_name_input = page.locator(
                    "#merchant_bank_accounts_attributes_0_bank_branch_name, "
                    "#merchant_bank_accounts_attributes_0_branch_name"
                ).first
                card_no_input = page.locator(
                    "#merchant_bank_accounts_attributes_0_account_no"
                ).first

                if info_type == "bank":
                    bank_values = (
                        info.get("bank_name", ""),
                        info.get("branch_name", ""),
                        info.get("bank_account", ""),
                    )
                else:
                    bank_values = (default_num, default_num, default_num)

                for loc, value in zip(
                    (bank_name_input, branch_name_input, card_no_input), bank_values
                ):
                    try:
                        if await loc.count() and await loc.is_visible():
                            await loc.fill(value)
                    except Exception:
                        pass

                # 支付宝 / 数字人民币
                alipay_input = page.locator(
                    "#merchant_alipay_accounts_attributes_0_account_name"
                ).first
                if await alipay_input.count() and await alipay_input.is_visible():
                    await alipay_input.fill(
                        info.get("alipay_account", "") if info_type == "alipay" else ""
                    )

                ecny_input = page.locator(
                    "#merchant_ecny_accounts_attributes_0_account_name"
                ).first
                if await ecny_input.count() and await ecny_input.is_visible():
                    await ecny_input.fill(
                        info.get("digital_account", "")
                        if info_type == "digital_wallet"
                        else ""
                    )

                # 商城界面
                shop_template = page.locator("#merchant_store_skin_type").first
                if await shop_template.count() and await shop_template.is_visible():
                    try:
                        if await shop_template.is_enabled():
                            try:
                                await shop_template.select_option(label=target_skin, timeout=5000)
                            except Exception:
                                try:
                                    await shop_template.select_option(index=1, timeout=5000)
                                except Exception as e:
                                    print(f"⚠️ 商城界面选择跳过：{e}")
                        else:
                            print("ℹ️ merchant_store_skin_type 当前为 disabled，保留后台默认商城界面。")
                    except Exception as e:
                        print(f"⚠️ 商城界面检测跳过：{e}")

                # 送出
                submit_btn = page.locator(
                    "input[name='commit'][value='送出'], "
                    "button[type='submit'], "
                    "input[type='submit']"
                ).first
                await submit_btn.wait_for(state="visible", timeout=10000)
                await submit_btn.click()

                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(500)

                body_text = await page.locator("body").inner_text()
                duplicate = any(
                    x in body_text
                    for x in [
                        "已经被使用",
                        "已經被使用",
                        "帐号已经存在",
                        "帳號已經存在",
                        "帐号已存在",
                        "帳號已存在",
                    ]
                )

                if duplicate:
                    suffix_num += 1
                    continue

                # 如果仍停留在建店表单，说明提交没有成功；不要无限循环。
                if "/merchants/new" in page.url:
                    errors = re.findall(
                        r"(?:错误|錯誤|失败|失敗|不能为空|不能為空|已经|已經)[^\n]{0,120}",
                        body_text,
                    )
                    detail = "；".join(errors[:3]) if errors else body_text[:500]
                    raise Exception(f"建店提交后仍停留在建立店铺页面：{detail}")

                final_account = current_account
                break
            # 只有真正离开 /merchants/new 建店页面，才认定本次帐号创建成功。

            # 3. 找到刚创建的商户
            await _single_search_account(page, final_account)
            shop_url = ""
            try:
                shop_url = (
                    await page.locator("tbody tr").first.locator("td").nth(3).inner_text()
                ).strip()
            except Exception:
                pass

            # 4. 商品 60
            await page.locator("tbody tr").first.locator("a[href$='items']").click()
            await page.wait_for_load_state("domcontentloaded")
            import_btn = page.locator(
                "a[href*='/items/new'], a[href='items/new'], "
                "a:has-text('導入商品'), a:has-text('导入商品')"
            ).first
            await import_btn.wait_for(state="visible", timeout=20000)
            await import_btn.click()
            await page.locator("#count_of_items, input[name='count_of_items']").fill("60")
            await page.locator(
                "input[name='commit'], input[value='送出'], button[type='submit']"
            ).first.click()
            await page.wait_for_load_state("domcontentloaded")

            # 5. 非银行付款移除默认银行卡占位符
            if info_type != "bank":
                await _single_search_account(page, final_account)
                await page.locator("tbody tr").first.locator("a[href$='edit']").click()
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
                    await page.locator(
                        "input[name='commit'][value='送出'], button[type='submit']"
                    ).first.click()
                    await page.wait_for_load_state("domcontentloaded")

            # 6. JJ 查询
            jj_result = await _query_jj_order(info["single_order_no"], task_id)

            # 7. 单笔商城充值
            recharge_result = await _single_recharge(
                page=page,
                account=final_account,
                jj_result=jj_result,
            )

            msg_text = (
                "✅ <b>单笔商城流程完成！</b>\n\n"
                f"店铺网址： <code>{html.escape(shop_url)}</code>\n"
                f"登入帐号： <code>{html.escape(final_account)}</code>\n"
                "登入密码： <code>a12345</code>\n"
                f"JJ订单状态： <b>{html.escape(jj_result['status'])}</b>\n"
                f"充值结果： <b>{html.escape(recharge_result)}</b>"
            )
            return msg_text, final_account

        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def _jj_open_outbound(page):
    candidates = [
        "a:has-text('出货管理')",
        "a:has-text('出貨管理')",
        "a[href='guest_payment_orders']",
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
    if "guest_payment_orders" not in page.url:
        raise Exception("JJ 后台找不到【出货管理】页面入口")


async def _jj_unlock_search_range(page):
    """解开 JJ 出货管理的日期查询范围暗锁。

    截图确认页面使用 toggle-order-search-days-btn-placeholder / lock-btn，
    锁图标本身有时会带 hide，因此优先点击外层可见的占位/按钮。
    """
    selectors = [
        ".toggle-order-search-days-btn-placeholder",
        ".toggle-search-days-btn-placeholder",
        "button.toggle-order-search-days-btn",
        "a.toggle-order-search-days-btn",
        ".lock-btn:not(.hide)",
        ".fa-lock.lock-btn:not(.hide)",
        ".unlock-btn:not(.hide)",
    ]

    # 已经解锁时直接返回。
    try:
        if await page.locator(".fa-unlock.unlock-btn:not(.hide), .unlock-btn:not(.hide)").first.is_visible():
            return
    except Exception:
        pass

    for selector in selectors:
        loc = page.locator(selector).first
        try:
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=5000)
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue

    # 最后尝试找到包含 fa-lock 的可见父元素，用 JS 点击真正的可点击节点。
    try:
        lock_icon = page.locator("i.fa-lock").first
        if await lock_icon.count() and await lock_icon.is_visible():
            parent = lock_icon.locator("xpath=..")
            if await parent.count() and await parent.is_visible():
                await parent.click(timeout=5000)
                await page.wait_for_timeout(500)
                return
            await lock_icon.evaluate("el => el.closest('button,a,div')?.click()")
            await page.wait_for_timeout(500)
            return
    except Exception:
        pass


async def _jj_set_one_year_date(page):
    """JJ 出货管理：建立日期设为当前时间往前 1 年到当前时间。

    根据实际 DevTools 已确认：
      开始时间：#q_created_at_gte / name="q[created_at_gte]"
      结束时间：#q_created_at_lte / name="q[created_at_lte]"
    页面实际 value 使用 ISO 格式，例如：2026-09-15T03:00:00+08:00。
    """
    from datetime import timezone

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    start_dt = now - timedelta(days=365)

    start_value = start_dt.isoformat(timespec="seconds")
    end_value = now.isoformat(timespec="seconds")

    start = page.locator("#q_created_at_gte")
    end = page.locator("#q_created_at_lte")

    # 等待实际 DOM 出现。不能先用 count()，因为日期插件可能稍后才渲染。
    try:
        await start.wait_for(state="attached", timeout=10000)
        await end.wait_for(state="attached", timeout=10000)
    except Exception:
        # name 是同一个实际字段，再给一次明确的备用定位。
        start = page.locator("input[name='q[created_at_gte]']")
        end = page.locator("input[name='q[created_at_lte]']")
        await start.wait_for(state="attached", timeout=10000)
        await end.wait_for(state="attached", timeout=10000)

    async def set_datetime(locator, value):
        # JJ 使用 datetimepicker，普通 fill 有时会被插件拦截。
        # 直接设置 value，再触发 input/change/blur，让表单与插件同步。
        await locator.evaluate(
            """(el, value) => {
                const setter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
            }""",
            value,
        )

    # 解锁后日期栏应该可以操作；如果仍 disabled，重新点击暗锁一次。
    for _ in range(2):
        try:
            if await start.is_disabled() or await end.is_disabled():
                await _jj_unlock_search_range(page)
                await page.wait_for_timeout(500)
                continue
            break
        except Exception:
            break

    try:
        await start.scroll_into_view_if_needed(timeout=3000)
        await end.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass

    # 不依赖 visible；截图确认这两个 input 就是实际提交字段。
    await set_datetime(start, start_value)
    await set_datetime(end, end_value)

    # 验证实际 DOM value，避免“代码执行了但日期没写进去”。
    actual_start = await start.input_value()
    actual_end = await end.input_value()
    if actual_start != start_value or actual_end != end_value:
        # 再用 JS property setter 强制写一次。
        await set_datetime(start, start_value)
        await set_datetime(end, end_value)
        actual_start = await start.input_value()
        actual_end = await end.input_value()

    if actual_start != start_value or actual_end != end_value:
        raise Exception(
            f"JJ 建立日期写入失败：开始={actual_start}，结束={actual_end}"
        )

    return start_value, end_value


async def _jj_find_order_input(page, kind):
    """尽量兼容 JJ 后台实际页面的输入框写法。

    JJ 页面不同版本可能没有固定的 name/id，不能只靠猜测的选择器。
    优先使用 id/name/placeholder，其次根据「平台订单号/其他订单号」附近的文字寻找输入框。
    """
    if kind == "platform":
        labels = ["平台订单号", "平台訂單號", "平台订单", "平台訂單"]
        selectors = [
            "#q_id",
            "input[name='q[id]']",
            "input[name='q_id']",
            "#platform_order",
            "#platform_order_no",
            "#platform_order_number",
            "input[name='platform_order']",
            "input[name='platform_order_no']",
            "input[name='platform_order_number']",
            "input[placeholder*='平台订单号']",
            "input[placeholder*='平台訂單號']",
        ]
    else:
        labels = ["其他订单号", "其他訂單號", "其他订单", "其他訂單"]
        selectors = [
            "#q_merchant_order_id_or_order_trade_id",
            "input[name='q[merchant_order_id_or_order_trade_id]']",
            "input[name='q_merchant_order_id_or_order_trade_id']",
            "#other_order",
            "#other_order_no",
            "#other_order_number",
            "input[name='other_order']",
            "input[name='other_order_no']",
            "input[name='other_order_number']",
            "input[placeholder*='其他订单号']",
            "input[placeholder*='其他訂單號']",
        ]

    loc = await _first_visible(page, selectors, timeout=1500)
    if loc:
        return loc

    # 通过 label 的 for / 同级 input 寻找。
    for txt in labels:
        try:
            label = page.locator("label").filter(has_text=txt).first
            if await label.count() and await label.is_visible():
                target_id = await label.get_attribute("for")
                if target_id:
                    target = page.locator(f"#{target_id}").first
                    if await target.count() and await target.is_visible():
                        return target
                parent = label.locator("xpath=..")
                target = parent.locator("input:not([type='hidden'])").first
                if await target.count() and await target.is_visible():
                    return target
                target = parent.locator("textarea").first
                if await target.count() and await target.is_visible():
                    return target
        except Exception:
            pass

    # 有些 JJ 版本把标题放在 div/span，而不是 label。
    # 找到包含目标文字的元素后，在它的父/祖先容器内寻找第一个可见输入框。
    for txt in labels:
        try:
            text_node = page.get_by_text(txt, exact=False).first
            if await text_node.count() and await text_node.is_visible():
                for level in range(1, 5):
                    ancestor = text_node.locator("xpath=" + "/.." * level)
                    inp = ancestor.locator(
                        "input:not([type='hidden']):not([type='checkbox']):not([type='radio']), textarea"
                    ).first
                    if await inp.count() and await inp.is_visible():
                        return inp
        except Exception:
            pass

    # 最后按「输入框所在容器文字」做启发式匹配。
    inputs = page.locator(
        "input:not([type='hidden']):not([type='checkbox']):not([type='radio']):not([type='submit']), textarea"
    )
    count = await inputs.count()
    for i in range(count):
        inp = inputs.nth(i)
        try:
            if not await inp.is_visible() or await inp.is_disabled():
                continue
            bits = []
            for level in range(1, 4):
                ancestor = inp.locator("xpath=" + "/.." * level)
                try:
                    bits.append(await ancestor.inner_text(timeout=500))
                except Exception:
                    pass
            context_text = " ".join(bits)
            if any(txt in context_text for txt in labels):
                return inp
        except Exception:
            continue

    # 把当前页面实际存在的可见输入框信息放进错误，方便下一次直接定位，而不是卡住。
    available = []
    for i in range(min(count, 30)):
        inp = inputs.nth(i)
        try:
            if not await inp.is_visible():
                continue
            available.append({
                "id": await inp.get_attribute("id"),
                "name": await inp.get_attribute("name"),
                "placeholder": await inp.get_attribute("placeholder"),
            })
        except Exception:
            pass

    raise Exception(f"JJ 找不到【{labels[0]}】输入框；当前可见输入框：{available}")


async def _jj_search(page, order_no, kind):
    inp = await _jj_find_order_input(page, kind)
    await inp.fill(str(order_no).strip())

    # 截图已确认搜索表单 id=guest_payment_order_search。
    form = page.locator("#guest_payment_order_search").first
    if await form.count():
        search_btn = form.locator(
            "input[type='submit'], button[type='submit'], "
            "input[value='搜尋'], input[value='搜索'], "
            "button:has-text('搜尋'), button:has-text('搜索')"
        ).first
    else:
        search_btn = page.locator(
            "input[type='submit'][value='搜尋'], input[type='submit'][value='搜索'], "
            "button[type='submit']"
        ).first

    try:
        if await search_btn.count() and await search_btn.is_visible():
            await search_btn.click(timeout=10000)
        else:
            await inp.press("Enter")
    except Exception:
        await inp.press("Enter")

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(1000)



def _normalize_header(text):
    return re.sub(r'\s+', '', text or "").lower()


async def _extract_jj_row(page, order_no=""):
    """只返回真正包含目标订单号的结果，避免读取到旧表格第一行。"""
    tables = page.locator("table")
    table_count = await tables.count()
    for ti in range(table_count):
        table = tables.nth(ti)
        try:
            if not await table.is_visible():
                continue
            rows = table.locator("tbody tr")
            row_count = await rows.count()
            if row_count == 0:
                continue
            headers = table.locator("thead th")
            header_count = await headers.count()
            header_texts = [
                _normalize_header(await headers.nth(i).inner_text())
                for i in range(header_count)
            ]
            for ri in range(row_count):
                row = rows.nth(ri)
                cells = row.locator("td")
                if await cells.count() == 0:
                    continue
                row_text = _clean_text_value(await row.inner_text())
                compact_row = re.sub(r"\s+", "", row_text)
                compact_order = re.sub(r"\s+", "", str(order_no or ""))
                if order_no and compact_order not in compact_row:
                    continue
                cell_texts = [
                    _clean_text_value(await cells.nth(i).inner_text())
                    for i in range(await cells.count())
                ]
                return header_texts, cell_texts
        except Exception:
            continue
    return [], []


async def _query_jj_order(single_order_no, task_id):
    if not JJ_ADMIN_URL:
        raise Exception("未检测到环境变量 JJ_ADMIN_URL！")
    if not JJ_ADMIN_USER or not JJ_ADMIN_PASS:
        raise Exception("未检测到 JJ_ADMIN_USER 或 JJ_ADMIN_PASS！")
    if not single_order_no:
        raise Exception("单笔订单号为空！")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-blink-features=AutomationControlled',
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

            await _jj_search(page, single_order_no, "platform")
            headers, cells = await _extract_jj_row(page, single_order_no)

            if not cells:
                await _jj_search(page, single_order_no, "other")
                headers, cells = await _extract_jj_row(page, single_order_no)

            if not cells:
                raise Exception(f"JJ 找不到订单：{single_order_no}")

            status = _cell_by_header(headers, cells, ["状态", "狀態"])
            status = status or _clean_text_value(await page.locator("table tbody tr").first.inner_text())

            is_success = "成功" in status or "成功" in "".join(cells)
            is_failed = "失败" in status or "失敗" in status or "失败" in "".join(cells)

            order_no = _cell_by_header(headers, cells, ["平台订单", "平台訂單", "订单号", "訂單號"])
            recipient = _cell_by_header(headers, cells, ["商户会员", "商戶會員", "实名", "實名", "收件人", "收件人姓名"])
            amount = _cell_by_header(headers, cells, ["交易金额", "交易金額", "金额", "金額"])
            shipment = _cell_by_header(headers, cells, ["货运", "貨運", "运单", "運單", "货号", "貨號"])
            created = _cell_by_header(headers, cells, ["提交时间", "提交時間", "建立时间", "建立時間", "创建时间", "創建時間"])
            completed = _cell_by_header(headers, cells, ["完成时间", "完成時間"])

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
    await _single_search_account(page, account)

    recharge_link = page.locator(
        "a:has-text('充值管理'), a:has-text('商户充值管理'), "
        "a[href$='deposits/new'], a[href$='deposit']"
    ).first

    if not await recharge_link.is_visible():
        menu = page.locator("a:has-text('商户充值管理'), a:has-text('商戶充值管理')").first
        if await menu.count() and await menu.is_visible():
            await menu.click()
        else:
            raise Exception("单笔商城找不到【充值管理】入口")
    else:
        await recharge_link.click()

    await page.wait_for_load_state("domcontentloaded")

    merchant_select = await _first_visible(page, [
        "#deposit_order_merchant_id",
        "select[name='deposit_order[merchant_id]']",
        "select[name='merchant_id']",
    ], timeout=5000)
    if merchant_select:
        try:
            await merchant_select.select_option(label=account)
        except Exception:
            try:
                await merchant_select.select_option(value=account)
            except Exception:
                try:
                    await merchant_select.click()
                    await merchant_select.press("ArrowDown")
                    await merchant_select.press("Enter")
                except Exception:
                    pass

    recipient_info = await _first_visible(page, [
        "#deposit_order_recipient_info_id",
        "select[name='recipient_info']",
        "select[name='recipient[info]']",
    ], timeout=3000)
    if recipient_info:
        await _select_any_option(recipient_info)

    buyer_comment = page.locator(
        "#deposit_order_buyer_comment, textarea[name='buyer_comment']"
    ).first
    try:
        if await buyer_comment.is_visible():
            await buyer_comment.fill("")
    except Exception:
        pass

    shipment = jj_result.get("shipment", "")
    if shipment:
        await _fill_by_label(
            page,
            ["运单号", "運單號", "货运", "貨運"],
            shipment,
            required=False,
        )
        loc = page.locator(
            "#deposit_order_shipment_no, input[name='shipment_no'], input[name='shipment']"
        ).first
        try:
            if await loc.is_visible():
                await loc.fill(shipment)
        except Exception:
            pass

    recipient = jj_result.get("recipient") or MANAGER_RECEIVE_NAME
    await _fill_by_label(
        page,
        ["收件人姓名", "收件人", "实名", "實名"],
        recipient,
        required=False,
    )
    loc = page.locator(
        "#deposit_order_recipient_name, input[name='recipient_name']"
    ).first
    try:
        if await loc.is_visible():
            await loc.fill(recipient)
    except Exception:
        pass

    amount = jj_result.get("amount", "")
    if amount:
        await _fill_by_label(page, ["金额", "金額", "交易金额", "交易金額"], amount, required=False)
        loc = page.locator(
            "#deposit_order_total_amount, input[name='total_amount'], input[name='amount']"
        ).first
        try:
            if await loc.is_visible():
                await loc.fill(re.sub(r'[^\d.]', '', amount))
        except Exception:
            pass

    delivery = jj_result.get("delivery")
    if delivery:
        delivery_text = delivery.strftime("%Y%m%d %H%M")
        await _fill_by_label(
            page,
            ["配送时间", "配送時間"],
            delivery_text,
            required=False,
        )
        loc = page.locator(
            "#deposit_order_delivery_time, input[name='delivery_time']"
        ).first
        try:
            if await loc.is_visible():
                await loc.fill(delivery_text)
        except Exception:
            pass

    created = jj_result.get("created")
    if created:
        loc = page.locator(
            "#deposit_order_created_at, input[name='created_at']"
        ).first
        try:
            if await loc.is_visible() and not await loc.input_value():
                await loc.fill(created)
        except Exception:
            pass

    submit = page.locator(
        "button[type='submit'], input[type='submit'][value='送出'], input[type='submit'], "
        "button:has-text('送出')"
    ).last
    if not await submit.is_visible():
        raise Exception("找不到单笔商城充值的【送出】按钮")

    await submit.click()
    await page.wait_for_load_state("domcontentloaded")
    return "已送出"


# 修改商城界面函数
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
                "#admin_user_email, #user_email, input[type='email'], input[name='email'], input[name='login'], input[name='username'], input[type='text']"
            ).first
            await user_input.fill(ADMIN_USER)
            await page.locator("#admin_user_password, #user_password, input[type='password']").first.fill(ADMIN_PASS)
            await page.locator("input[type='submit'], button[type='submit'], input[name='commit']").first.click()
            await page.wait_for_load_state("domcontentloaded")

            domain_root = "/".join(BASE_ADMIN_URL.split("/")[:3])
            await page.goto(f"{domain_root}/merchants", wait_until="domcontentloaded")
            search_input = page.locator("input[name='account'], #search_account, input[type='search'], input[type='text']").first
            await search_input.fill(account_name)
            search_btn = page.locator("button:has-text('搜尋'), button:has-text('搜索'), input[type='submit'], .btn-primary").first
            if await search_btn.is_visible():
                await search_btn.click()
            else:
                await search_input.press("Enter")
            await page.locator("tbody tr").first.wait_for(state="visible", timeout=20000)

            await page.locator("tbody tr").first.locator("a[href$='edit']").click()
            await page.wait_for_load_state("domcontentloaded")

            shop_template = page.locator("#merchant_store_skin_type")
            if await shop_template.is_visible():
                try:
                    await shop_template.select_option(label=new_skin)
                except Exception:
                    await shop_template.select_option(label=f"预设{new_skin}")

            await page.locator("input[name='commit'][value='送出'], button[type='submit']").first.click()
            await page.wait_for_load_state("domcontentloaded")
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# 主按钮键盘
def build_main_keyboard(account: str, current_skin: str = "极速微商") -> InlineKeyboardMarkup:
    current_skin = current_skin.replace("预设", "")
    buttons = [
        [InlineKeyboardButton(f"✨ 更改商城界面（当前：{current_skin}）", callback_data=f"op|{account}|{current_skin}")]
    ]
    return InlineKeyboardMarkup(buttons)


# 风格键盘
def build_skin_options_keyboard(account: str, current_skin: str = "极速微商") -> InlineKeyboardMarkup:
    current_skin = current_skin.replace("预设", "")
    buttons = [
        [
            InlineKeyboardButton("极速微商", callback_data=f"sk|jisumeishang|{account}"),
            InlineKeyboardButton("七喵", callback_data=f"sk|qimiao|{account}")
        ],
        [
            InlineKeyboardButton("柒月", callback_data=f"sk|qiyue|{account}"),
            InlineKeyboardButton("音你而来", callback_data=f"sk|yinnierlai|{account}")
        ],
        [
            InlineKeyboardButton("⬅️ 收起", callback_data=f"cl|{account}|{current_skin}")
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
        [InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel|{task_id}")]
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


# Worker 包装
async def run_shop_worker(status_msg, parsed_info, task_id: str, is_single=False):
    try:
        if BUILD_SHOP_SEMAPHORE.locked():
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel|{task_id}")]
            ])
            await status_msg.edit_text(
                "⏳ <b>前方有建店任务正在处理中，已为您自动加入排队队列，请稍候...</b>",
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        async with BUILD_SHOP_SEMAPHORE:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel|{task_id}")]
            ])
            task_status_text = (
                "⏳ <b>已轮到当前单笔任务，正在自动建店中，请稍候...</b>"
                if is_single
                else "⏳ <b>已轮到当前任务，正在自动建店中，请稍候...</b>"
            )
            await status_msg.edit_text(
                task_status_text,
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
            f"❌ 建店出现错误：{safe_err}",
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

    if data.startswith("cancel"):
        task_id = data.split("|", 1)[1]

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

    elif data.startswith("op"):
        _, account, current_skin = data.split("|", 2)
        keyboard = build_skin_options_keyboard(account, current_skin)
        await query.edit_message_reply_markup(reply_markup=keyboard)
        await query.answer()

    elif data.startswith("cl"):
        _, account, current_skin = data.split("|", 2)
        keyboard = build_main_keyboard(account, current_skin)
        await query.edit_message_reply_markup(reply_markup=keyboard)
        await query.answer()

    elif data.startswith("sk"):
        _, skin_key, account = data.split("|", 2)
        new_skin_name = SKIN_OPTIONS.get(skin_key, "极速微商")

        await query.answer(f"⏳ 正在切换界面为【{new_skin_name}】...", show_alert=False)

        loading_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⏳ 正在切换为【{new_skin_name}】...", callback_data="ignore")]
        ])
        await query.edit_message_reply_markup(reply_markup=loading_keyboard)

        try:
            await update_shop_skin(account, new_skin_name)
            keyboard = build_main_keyboard(account, new_skin_name)
            await query.edit_message_reply_markup(reply_markup=keyboard)
            await query.answer(f"✅ 界面已成功更改为：{new_skin_name}", show_alert=True)
        except Exception as e:
            keyboard = build_skin_options_keyboard(account)
            await query.edit_message_reply_markup(reply_markup=keyboard)
            await query.answer(f"❌ 修改界面失败：{str(e)}", show_alert=True)


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
