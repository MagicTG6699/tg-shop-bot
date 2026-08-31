import os
import re
import asyncio
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from playwright.async_api import async_playwright

# ----------------- 环境变量配置 (无硬编码) -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# 一般商城后台配置 (无订单号时)
MALL_ADMIN_URL = os.getenv("MALL_ADMIN_URL", "https://asdtvheq.com/admin")
MALL_ADMIN_ACCOUNT = os.getenv("MALL_ADMIN_ACCOUNT") or os.getenv("ADMIN_USER")
MALL_ADMIN_PASSWORD = os.getenv("MALL_ADMIN_PASSWORD") or os.getenv("ADMIN_PASS")

# 单笔/三笔 市场管理后台配置 (1~3笔订单号时)
SINGLE_ORDER_ADMIN_URL = os.getenv("SINGLE_ORDER_ADMIN_URL", "https://asdtvheq.com/market_managers/sign_in")
SINGLE_ORDER_ACCOUNT = os.getenv("SINGLE_ORDER_ACCOUNT") or os.getenv("MARKET_USER")
SINGLE_ORDER_PASSWORD = os.getenv("SINGLE_ORDER_PASSWORD") or os.getenv("MARKET_PASS")

# 自动推导基础域名 (例如 https://asdtvheq.com)
BASE_DOMAIN = re.sub(r'/(admin|market_managers).*$', '', SINGLE_ORDER_ADMIN_URL)

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None
UUID_PATTERN = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

cancel_flags = {}

def extract_order_ids(text):
    return re.findall(UUID_PATTERN, text)

def parse_and_validate_text(text: str) -> tuple[dict, str]:
    info = {}

    digital_keywords = [
        "数字R人民币", "數字R人民幣", "数字R", "數字R",
        "数字人民币", "數字人民幣", "数币", "數幣",
        "数字", "數字", "钱包", "錢包", "ecny"
    ]
    bank_keywords = ["银行", "銀行", "卡号", "卡號"]
    alipay_keywords = ["支付宝", "支付寶", "支"]

    if any(k in text for k in digital_keywords):
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

    for line in clean_text.splitlines():
        line = line.strip()
        if not line:
            continue

        parts = re.split(r'[:：]', line, maxsplit=1)
        if len(parts) < 2:
            continue

        key = re.sub(r'\s+', '', parts[0])
        val = parts[1].strip()
        val = re.sub(r'^[<\("‘“]+|[>\)"’”]+$', '', val)

        if "登入" not in key and (
            any(k in key for k in ["盖平台", "平台", "会员", "會員"])
            or key in ["账号", "帳號", "帐号", "会员号", "會員號", "平台账号", "平台帳號"]
        ):
            if not any(k in key for k in ["支付宝", "支付寶", "银行", "銀行", "数字", "數字", "钱包", "錢包"]):
                info["account"] = val.lower()

        elif any(k in key for k in ["户名", "戶名", "姓名", "名字", "客户姓名", "客戶姓名"]) or key in ["名", "数字人民币户名", "數字人民幣戶名"]:
            info["name"] = val

        elif any(k in key for k in ["手机", "手機", "电话", "電話", "联系方式"]):
            raw_phone = val

        elif any(k in key for k in alipay_keywords):
            raw_accounts["alipay"] = val

        elif any(k in key for k in digital_keywords):
            raw_accounts["digital"] = val

        elif "银行名称" in key or "銀行名稱" in key:
            if "-" in val:
                bank_parts = val.split("-")
                info["bank_name"] = bank_parts[0].strip()
                info["branch_name"] = bank_parts[1].strip()
            else:
                info["bank_name"] = val
                info["branch_name"] = val
        elif any(k in key for k in bank_keywords):
            raw_accounts["bank"] = val

    errors = []

    if not info.get("account"):
        errors.append("• 未提取到【平台会员账号】！")

    if raw_phone:
        if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_phone):
            errors.append(f"• 手机号错误：`{raw_phone}`（只允许数字，不能包含英文或中文）")
        else:
            digits_phone = re.sub(r'\D', '', raw_phone)
            if len(digits_phone) < 11:
                errors.append(f"• 手机号位数错误：`{raw_phone}`（最少必须为 11 位数字）")
            else:
                info["phone"] = digits_phone

    info_type = info.get("type")

    if info_type == "digital_wallet":
        raw_val = raw_accounts.get("digital")
        if not raw_val:
            errors.append("• 未找到【数字人民币账号】！")
        else:
            if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_val):
                errors.append(f"• 数字人民币账号错误：`{raw_val}`（只允许数字，不能包含英文或中文）")
            else:
                digits = re.sub(r'\D', '', raw_val)
                if not digits:
                    errors.append(f"• 数字人民币账号无效：`{raw_val}`")
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
                    errors.append(f"• 支付宝邮箱格式错误：`{raw_val}`")
            else:
                if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_val):
                    errors.append(f"• 支付宝账号错误：`{raw_val}`（仅支持手机号或邮箱格式）")
                else:
                    digits = re.sub(r'\D', '', raw_val)
                    if not digits:
                        errors.append(f"• 支付宝账号无效：`{raw_val}`")
                    else:
                        info["alipay_account"] = digits
        else:
            if not info.get("phone") and not errors:
                errors.append("• 缺失支付宝账号及手机号！")
            elif info.get("phone"):
                info["alipay_account"] = info.get("phone")

    elif info_type == "bank":
        raw_val = raw_accounts.get("bank")
        if not raw_val:
            errors.append("• 未找到【银行卡号】！")
        else:
            if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_val):
                errors.append(f"• 银行卡号错误：`{raw_val}`（只允许数字，不能包含英文或中文）")
            else:
                digits = re.sub(r'\D', '', raw_val)
                if not digits:
                    errors.append(f"• 银行卡号无效：`{raw_val}`")
                else:
                    info["bank_account"] = digits

    if errors:
        error_summary = "❌ **建店失败！检测到以下输入错误：**\n\n" + "\n".join(errors)
        return None, error_summary

    return info, ""

def build_cancel_keyboard(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel_build_{chat_id}"))
    return markup

# ---------------- 1. 单笔/三笔 市场管理后台 ----------------
async def run_playwright_single_order_build(info, order_ids):
    base_account = info.get("account")
    suffix_num = 0
    final_account = base_account

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(30000)

        print(f"[Playwright] 正在访问单笔后台: {SINGLE_ORDER_ADMIN_URL}", flush=True)
        await page.goto(SINGLE_ORDER_ADMIN_URL, wait_until="domcontentloaded")
        
        # 登录
        await page.locator("input[type='text'], input[type='email']").first.fill(SINGLE_ORDER_ACCOUNT)
        await page.locator("input[type='password']").first.fill(SINGLE_ORDER_PASSWORD)
        await page.locator("input[type='submit'], button[type='submit']").first.click()
        await page.wait_for_url(lambda url: "/sign_in" not in url, timeout=20000)
        print(f"[Playwright] 单笔后台登录成功，当前URL: {page.url}", flush=True)

        async def search_account(account_name: str):
            merchants_url = f"{BASE_DOMAIN}/market_managers/merchants"
            await page.goto(merchants_url, wait_until="domcontentloaded")
            search_input = page.locator("input[name*='account'], #search_account, input[type='search'], input[type='text']").first
            await search_input.wait_for(state="visible", timeout=15000)
            await search_input.fill(account_name)
            try:
                search_btn = page.locator("button:has-text('搜尋'), button:has-text('搜索'), input[type='submit'], .btn-primary").first
                if await search_btn.is_visible():
                    await search_btn.click()
                else:
                    await search_input.press("Enter")
            except Exception:
                await search_input.press("Enter")
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)

        # 2. 建店循环
        while True:
            current_account = base_account if suffix_num == 0 else f"{base_account}{suffix_num:02d}"
            print(f"[Playwright] 尝试创建店铺账号: {current_account}", flush=True)
            
            # 访问列表页
            merchants_url = f"{BASE_DOMAIN}/market_managers/merchants"
            await page.goto(merchants_url, wait_until="domcontentloaded")
            
            # 多重兼容匹配新建按钮
            new_btn_selectors = [
                "a[href$='/merchants/new']",
                "a[href*='/merchants/new']",
                "a:has-text('建立店铺')",
                "a:has-text('建立店鋪')",
                "a:has-text('新增店铺')",
                "a:has-text('新增店鋪')",
                "a:has-text('建立商家')",
                "a:has-text('新增商家')",
                ".btn-primary:has-text('建立')",
                ".btn-primary:has-text('新增')"
            ]
            
            btn_found = False
            for sel in new_btn_selectors:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    btn_found = True
                    break
            
            if not btn_found:
                # 备用：如果列表页找不到按钮，直接拼 URL 跳转
                print("[Playwright] 没找到建店按钮，尝试直接访问 /new 路径...", flush=True)
                await page.goto(f"{BASE_DOMAIN}/market_managers/merchants/new", wait_until="domcontentloaded")

            # 等待账号表单加载
            username_input = page.locator("input[name='merchant[username]'], #merchant_username, input[id*='username']").first
            try:
                await username_input.wait_for(state="visible", timeout=15000)
            except Exception as e:
                print(f"[Playwright 调试信息] 当前页面真实 URL: {page.url}", flush=True)
                raise e

            await username_input.fill(current_account)
            
            password_input = page.locator("#merchant_password, input[name='merchant[password]'], input[id*='password']").first
            if await password_input.count() > 0 and await password_input.first.is_visible():
                await password_input.first.fill("a12345")
                
            confirm_input = page.locator("#merchant_password_confirmation, input[name='merchant[password_confirmation]']").first
            if await confirm_input.count() > 0 and await confirm_input.first.is_visible():
                await confirm_input.first.fill("a12345")

            platform_select = page.locator("#merchant_sprite_platform, select[name*='sprite_platform']").first
            if await platform_select.count() > 0 and await platform_select.is_visible():
                try:
                    await platform_select.select_option(label="jj")
                except Exception:
                    await platform_select.select_option(value="jj")

            name_input = page.locator("#merchant_account_name, input[name*='account_name']").first
            if await name_input.count() > 0 and await name_input.is_visible():
                await name_input.fill(info.get("name", ""))

            phone_input = page.locator("#merchant_phone, input[name*='phone']").first
            if await phone_input.count() > 0 and await phone_input.is_visible():
                await phone_input.fill(info.get("phone", ""))

            info_type = info.get("type", "alipay")
            default_num = "6226220809397366"

            bank_name_input = page.locator("#merchant_bank_accounts_attributes_0_bank_name, input[id$='_bank_name']").first
            branch_name_input = page.locator("#merchant_bank_accounts_attributes_0_branch_name, input[id$='_branch_name']").first
            card_no_input = page.locator("#merchant_bank_accounts_attributes_0_account_no, input[id$='_account_no']").first

            if info_type == "bank":
                if await bank_name_input.is_visible(): await bank_name_input.fill(info.get("bank_name", ""))
                if await branch_name_input.is_visible(): await branch_name_input.fill(info.get("branch_name", ""))
                if await card_no_input.is_visible(): await card_no_input.fill(info.get("bank_account", ""))
            else:
                if await bank_name_input.is_visible(): await bank_name_input.fill(default_num)
                if await branch_name_input.is_visible(): await branch_name_input.fill(default_num)
                if await card_no_input.is_visible(): await card_no_input.fill(default_num)

            alipay_input = page.locator("#merchant_alipay_accounts_attributes_0_account_name, input[id*='alipay']").first
            if info_type == "alipay":
                if await alipay_input.is_visible(): await alipay_input.fill(info.get("alipay_account", ""))
            else:
                if await alipay_input.is_visible(): await alipay_input.fill("")

            ecny_input = page.locator("#merchant_ecny_accounts_attributes_0_account_name, input[id*='ecny']").first
            if info_type == "digital_wallet":
                if await ecny_input.is_visible(): await ecny_input.fill(info.get("digital_account", ""))
            else:
                if await ecny_input.is_visible(): await ecny_input.fill("")

            # 提交表单
            await page.locator("input[name='commit'][value='送出'], button[type='submit']:has-text('送出'), input[type='submit']").first.click()
            await page.wait_for_load_state("domcontentloaded")

            # 检查重复账号提示
            is_used = await page.locator("body").evaluate("el => el.innerText.includes('已经被使用') || el.innerText.includes('已經被使用')")
            if is_used:
                print(f"[Playwright] 账号 {current_account} 已被使用，尝试递增后缀", flush=True)
                suffix_num += 1
                continue
            else:
                final_account = current_account
                break

        print(f"[Playwright] 建店成功，最终账号: {final_account}", flush=True)

        # 3. 获取店铺网址
        await search_account(final_account)
        shop_url = (await page.locator("tbody tr").first.locator("td").nth(3).inner_text()).strip()

        # 4. 绑定单笔/三笔订单号 (出货订单)
        await page.locator("tbody tr").first.locator("a[href$='/deposits']").click()
        for oid in order_ids:
            new_btn = page.locator("a[href$='/deposits/new'], a:has-text('輸入出貨訂單'), a:has-text('新增订单'), a:has-text('新增訂單')").first
            if await new_btn.is_visible():
                await new_btn.click()
                order_input = page.locator("input[name*='order'], #deposit_order_number, textarea[name*='order']").first
                await order_input.fill(oid)
                await page.locator("input[name='commit'], input[value='送出']").click()
                await asyncio.sleep(1)

        await browser.close()
        return shop_url, final_account

# ---------------- 2. 一般商城后台 (无单号) ----------------
async def run_playwright_general_build(info):
    base_account = info.get("account")
    suffix_num = 0
    final_account = base_account

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(30000)

        await page.goto(MALL_ADMIN_URL, wait_until="domcontentloaded")
        await page.locator("input[type='text'], input[type='email']").first.fill(MALL_ADMIN_ACCOUNT)
        await page.locator("input[type='password']").first.fill(MALL_ADMIN_PASSWORD)
        await page.locator("input[type='submit'], button[type='submit']").first.click()
        await page.wait_for_url(lambda url: "/users/sign_in" not in url, timeout=20000)

        async def search_account(account_name: str):
            merchants_url = f"{BASE_DOMAIN}/admin/merchants"
            await page.goto(merchants_url, wait_until="domcontentloaded")
            search_input = page.locator("input[name*='account'], #search_account, input[type='search'], input[type='text']").first
            await search_input.wait_for(state="visible", timeout=15000)
            await search_input.fill(account_name)
            try:
                search_btn = page.locator("button:has-text('搜尋'), button:has-text('搜索'), input[type='submit'], .btn-primary").first
                if await search_btn.is_visible():
                    await search_btn.click()
                else:
                    await search_input.press("Enter")
            except Exception:
                await search_input.press("Enter")
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)

        while True:
            current_account = base_account if suffix_num == 0 else f"{base_account}{suffix_num:02d}"
            await page.goto(f"{BASE_DOMAIN}/admin/merchants/new", wait_until="domcontentloaded")
            await page.locator("#merchant_username").wait_for(state="visible", timeout=15000)

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
                if await bank_name_input.is_visible(): await bank_name_input.fill(info.get("bank_name", ""))
                if await branch_name_input.is_visible(): await branch_name_input.fill(info.get("branch_name", ""))
                if await card_no_input.is_visible(): await card_no_input.fill(info.get("bank_account", ""))
            else:
                if await bank_name_input.is_visible(): await bank_name_input.fill(default_num)
                if await branch_name_input.is_visible(): await branch_name_input.fill(default_num)
                if await card_no_input.is_visible(): await card_no_input.fill(default_num)

            alipay_input = page.locator("#merchant_alipay_accounts_attributes_0_account_name")
            if info_type == "alipay":
                if await alipay_input.is_visible(): await alipay_input.fill(info.get("alipay_account", ""))
            else:
                if await alipay_input.is_visible(): await alipay_input.fill("")

            ecny_input = page.locator("#merchant_ecny_accounts_attributes_0_account_name")
            if info_type == "digital_wallet":
                if await ecny_input.is_visible(): await ecny_input.fill(info.get("digital_account", ""))
            else:
                if await ecny_input.is_visible(): await ecny_input.fill("")

            shop_template = page.locator("#merchant_store_skin_type")
            if await shop_template.is_visible():
                try:
                    await shop_template.select_option(label="极速微商")
                except Exception:
                    try:
                        await shop_template.select_option(label="極速微商")
                    except Exception:
                        await shop_template.select_option(index=1)

            await page.locator("input[name='commit'][value='送出']").first.click()
            await page.wait_for_load_state("domcontentloaded")

            is_used = await page.locator("body").evaluate("el => el.innerText.includes('已经被使用') || el.innerText.includes('已經被使用')")
            if is_used:
                suffix_num += 1
                continue
            else:
                final_account = current_account
                break

        # 获取店铺网址
        await search_account(final_account)
        shop_url = (await page.locator("tbody tr").first.locator("td").nth(3).inner_text()).strip()

        # 导入 60 商品
        await page.locator("tbody tr").first.locator("a[href$='/items']").click()
        await page.locator("a[href*='/items/new'], a:has-text('導入商品')").first.click()
        await page.locator("#count_of_items, input[name='count_of_items']").fill("60")
        await page.locator("input[name='commit'], input[value='送出']").click()

        # 剔除占位卡
        if info_type != "bank":
            await search_account(final_account)
            await page.locator("tbody tr").first.locator("a[href$='/edit']").click()
            remove_btn = page.locator("a.remove_fields.dynamic, a:has-text('移除')").first
            if await remove_btn.is_visible():
                await remove_btn.click()
            await page.locator("input[name='commit'], input[value='送出']").click()

        # 出货 6000
        await search_account(final_account)
        await page.locator("tbody tr").first.locator("a[href$='/deposits']").click()
        await page.locator("a[href$='/deposits/new'], a:has-text('輸入出貨訂單')").first.click()
        await page.locator("#quantity, input[name='quantity']").fill("6000")
        await page.locator("input[name='commit'], input[value='送出']").click()

        # 提现 6000
        await search_account(final_account)
        await page.locator("tbody tr").first.locator("a[href$='/withdraws']").click()
        withdraw_btn = page.locator("a:has-text('輸入拼多多訂單'), a:has-text('輸入提現訂單'), a[href*='/withdraws/new']").first
        await withdraw_btn.click()
        await page.locator("#quantity, input[name='quantity']").fill("6000")
        await page.locator("input[name='commit'], input[value='送出']").click()

        await browser.close()
        return shop_url, final_account

def execute_auto_build(chat_id, status_msg_id, parsed_info, order_ids):
    try:
        if cancel_flags.get(chat_id, False):
            return

        if order_ids:
            shop_url, final_account = asyncio.run(run_playwright_single_order_build(parsed_info, order_ids))
        else:
            shop_url, final_account = asyncio.run(run_playwright_general_build(parsed_info))

        result_text = (
            f"✅ **建店完成！**\n\n"
            f"店铺网址: {shop_url}\n"
            f"登入帳號: {final_account}\n"
            f"登入密码: a12345"
        )

        bot.edit_message_text(result_text, chat_id=chat_id, message_id=status_msg_id, disable_web_page_preview=True)

    except Exception as e:
        print(f"[Exception Error]: {str(e)}", flush=True)
        bot.edit_message_text(f"❌ 建店失败: {str(e)}", chat_id=chat_id, message_id=status_msg_id)

@bot.message_handler(func=lambda msg: True)
def handle_message(message):
    text = message.text
    chat_id = message.chat.id
    order_ids = extract_order_ids(text)

    if len(order_ids) > 3:
        bot.reply_to(message, f"❌ 创建失败：单笔/三笔模式最多只支持 1~3 笔订单号！（当前检测到 {len(order_ids)} 笔）")
        return

    parsed_info, error_msg = parse_and_validate_text(text)
    if error_msg:
        bot.reply_to(message, error_msg)
        return

    cancel_flags[chat_id] = False

    status_tip = f"⌛ 正在处理【单笔/三笔 ({len(order_ids)}笔)】店铺..." if order_ids else "⌛ 正在自动创建【一般商城】..."
    status_msg = bot.reply_to(message, status_tip, reply_markup=build_cancel_keyboard(chat_id))

    execute_auto_build(chat_id, status_msg.message_id, parsed_info, order_ids)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_build_"))
def handle_cancel(call):
    chat_id = int(call.data.split("_")[-1])
    cancel_flags[chat_id] = True
    bot.edit_message_text("❌ 任务已被取消", chat_id=call.message.chat.id, message_id=call.message.message_id)
    bot.answer_callback_query(call.id, "已取消")

if __name__ == "__main__":
    if bot:
        print("Bot 已成功启动...", flush=True)
        bot.remove_webhook()
        bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
