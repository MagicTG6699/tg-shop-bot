import os
import sys
import asyncio
import re
import html
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

# 全局任务字典
ACTIVE_TASKS = {}


# 2. 文本解析与格式校验
def parse_and_validate_text(text: str) -> tuple[dict, str]:
    info = {}

    digital_keywords = [
        "数字R人民币", "數字R人民幣", "数字R", "數字R",
        "数字人民币", "數字人民幣", "数币", "數幣",
        "数字", "數字", "钱包", "錢包", "ecny"
    ]
    bank_keywords = ["银行", "銀行", "卡号", "卡號", "银", "銀", "卡"]
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

        # 平台账号
        if "登入" not in key and (
            any(k in key for k in ["盖平台", "平台", "会员", "會員"])
            or key in ["账号", "帳號", "帐号", "会员号", "會員號", "平台账号", "平台帳號", "平台会员账号", "平台會員帳號"]
        ):
            if not any(k in key for k in ["支付宝", "支付寶", "银行", "銀行", "数字", "數字", "钱包", "錢包"]):
                info["account"] = val.lower()

        # 姓名
        elif any(k in key for k in ["户名", "戶名", "姓名", "名字", "客户姓名", "客戶姓名"]) or key in ["名", "数字人民币户名", "數字人民幣戶名"]:
            info["name"] = val

        # 手机号
        elif any(k in key for k in ["手机", "手機", "电话", "電話", "联系方式"]):
            raw_phone = val

        # 支付宝
        elif any(k in key for k in alipay_keywords) and not any(k in key for k in ["支行", "开户支行", "银行支行"]):
            raw_accounts["alipay"] = val

        # 数字货币
        elif any(k in key for k in digital_keywords):
            raw_accounts["digital"] = val

        # 1. 优先提取：支行 / 分行 / 网点名称（优先级高于银行名称，防止误匹配）
        elif any(k in key for k in ["支行", "分行", "网点", "網點", "开户支行", "開戶支行", "银行支行", "銀行支行"]):
            info["branch_name"] = val

        # 2. 提取：银行名称（精准排除带有“支行”关键词的整行）
        elif any(k in key for k in ["银行名称", "銀行名稱", "开户行", "開戶行", "行名"]) or key in ["银行", "銀行"]:
            if "支行" not in key:
                if "-" in val or " " in val:
                    bank_parts = re.split(r'[- ]+', val, maxsplit=1)
                    info["bank_name"] = bank_parts[0].strip()
                    info["branch_name"] = bank_parts[1].strip()
                else:
                    info["bank_name"] = val

        # 3. 银行卡号解析
        elif any(k in key for k in ["银行账号", "銀行帳號", "银行卡号", "銀行卡號", "卡号", "卡號"]) or key in ["银", "銀"]:
            raw_accounts["bank"] = val

    errors = []

    if not info.get("account"):
        errors.append("• 未提取到【平台会员账号】！")

    if raw_phone:
        if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_phone):
            errors.append(f"• 手机号错误：<code>{html.escape(raw_phone)}</code>（只允许数字）")
        else:
            digits_phone = re.sub(r'\D', '', raw_phone)
            if len(digits_phone) < 11:
                errors.append(f"• 手机号位数错误：<code>{html.escape(raw_phone)}</code>（至少11位）")
            else:
                info["phone"] = digits_phone

    info_type = info.get("type")

    if info_type == "digital_wallet":
        raw_val = raw_accounts.get("digital")
        if not raw_val:
            errors.append("• 未找到【数字人民币账号】！")
        else:
            if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_val):
                errors.append(f"• 数字人民币账号错误：<code>{html.escape(raw_val)}</code>（只允许数字）")
            else:
                digits = re.sub(r'\D', '', raw_val)
                if not digits:
                    errors.append(f"• 数字人民币账号无效：<code>{html.escape(raw_val)}</code>")
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
                    errors.append(f"• 支付宝邮箱格式错误：<code>{html.escape(raw_val)}</code>")
            else:
                if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_val):
                    errors.append(f"• 支付宝账号错误：<code>{html.escape(raw_val)}</code>（仅支持手机号或邮箱）")
                else:
                    digits = re.sub(r'\D', '', raw_val)
                    if not digits:
                        errors.append(f"• 支付宝账号无效：<code>{html.escape(raw_val)}</code>")
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
            errors.append("• 未找到【银行卡号/账号】！")
        else:
            if re.search(r'[\u4e00-\u9fa5a-zA-Z]', raw_val):
                errors.append(f"• 银行卡号错误：<code>{html.escape(raw_val)}</code>（只允许数字）")
            else:
                digits = re.sub(r'\D', '', raw_val)
                if not digits:
                    errors.append(f"• 银行卡号无效：<code>{html.escape(raw_val)}</code>")
                else:
                    info["bank_account"] = digits

        # 强校验：缺少银行或支行时精准提示
        if not info.get("bank_name"):
            errors.append("• 缺少【银行名称】！")
        if not info.get("branch_name"):
            errors.append("• 缺少【支行名称】！")

    if errors:
        error_summary = "❌ <b>建店失败！检测到以下输入错误：</b>\n\n" + "\n".join(errors)
        return None, error_summary

    return info, ""


# 3. Playwright 自动化建店
async def create_and_setup_shop(info: dict, task_id: str) -> str:
    if not BASE_ADMIN_URL:
        raise Exception("未检测到环境变量 ADMIN_URL！")
    if not ADMIN_USER or not ADMIN_PASS:
        raise Exception("未检测到 ADMIN_USER / ADMIN_PASS！")

    base_account = info.get("account")
    suffix_num = 0
    final_account = base_account

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-blink-features=AutomationControlled']
        )
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

        try:
            # 1. 登录后台
            await page.goto(BASE_ADMIN_URL, wait_until="domcontentloaded")
            user_input = page.locator(
                "#admin_user_email, #user_email, input[type='email'], input[name*='email'], input[name*='login'], input[name*='username'], input[type='text']"
            ).first

            try:
                await user_input.wait_for(state="visible", timeout=20000)
            except Exception:
                raise Exception(f"无法找到登录框！标题:【{await page.title()}】，地址: {page.url}")

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

            # 5. 移除非银行卡占位符
            if info_type != "bank":
                async def step_remove_placeholder():
                    await search_account(final_account)
                    await page.locator("tbody tr").first.locator("a[href$='/edit']").click()
                    remove_btn = page.locator("a.remove_fields.dynamic, a:has-text('移除')").first
                    if await remove_btn.is_visible():
                        await remove_btn.click()
                    await page.locator("input[name='commit'], input[value='送出']").click()
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

            return (
                f"✅ <b>建店完成！</b>\n\n"
                f"店铺网址 : <code>{html.escape(shop_url)}</code>\n"
                f"登入帳號 : <code>{html.escape(final_account)}</code>\n"
                f"登入密码 : <code>a12345</code>"
            )
        except PlaywrightTimeoutError:
            raise Exception("建店关键流程超时，后台响应较慢，请稍后前往后台核对。")
        finally:
            await browser.close()


# 后台 Task 包装
async def run_shop_worker(status_msg, parsed_info, task_id: str):
    try:
        result_text = await create_and_setup_shop(parsed_info, task_id)
        await status_msg.edit_text(result_text, parse_mode="HTML", disable_web_page_preview=True)
    except asyncio.CancelledError:
        await status_msg.edit_text("🛑 <b>已取消建店！</b>", parse_mode="HTML")
    except Exception as e:
        safe_err = html.escape(str(e))
        await status_msg.edit_text(f"❌ 建店出现错误：{safe_err}", parse_mode="HTML", disable_web_page_preview=True)
    finally:
        ACTIVE_TASKS.pop(task_id, None)


# 4. Telegram 消息接收处理
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    if not user_text:
        return

    chat_type = update.effective_chat.type
    user_id = update.effective_user.id

    if chat_type == "private" and ADMIN_USER_IDS and (user_id not in ADMIN_USER_IDS):
        return

    ignore_keywords = ["店铺网址", "店鋪網址", "登入密碼", "登入密码", "极速微商", "極速微商"]
    if any(k in user_text for k in ignore_keywords):
        return

    trigger_keywords = ["账号", "帳號", "帐号", "盖平台", "平台", "平台账号", "平台帳號", "数字人民币", "數字人民幣", "数字", "數字", "支付宝", "支付寶", "银行", "銀行"]
    if not any(k in user_text for k in trigger_keywords):
        return

    parsed_info, error_msg = parse_and_validate_text(user_text)

    if error_msg:
        await update.message.reply_text(error_msg, parse_mode="HTML", disable_web_page_preview=True)
        return

    task_id = f"{update.message.chat_id}_{update.message.message_id}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel_{task_id}")]
    ])

    status_msg = await update.message.reply_text(
        "⏳ <b>正在自动建店中，请稍候...</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    task = asyncio.create_task(run_shop_worker(status_msg, parsed_info, task_id))

    ACTIVE_TASKS[task_id] = {
        "task": task,
        "page": None,
        "user_id": user_id
    }


# 5. 回调事件处理
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    click_user_id = query.from_user.id

    if data.startswith("cancel_"):
        task_id = data.replace("cancel_", "")

        if task_id not in ACTIVE_TASKS:
            await query.edit_message_text("⚠️ 该任务已结束或已被取消。")
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


# 6. 程序入口
def main():
    if not BOT_TOKEN:
        print("❌ 未检测到 BOT_TOKEN 环境变量，请在环境变量中设置 BOT_TOKEN 后重试！")
        sys.exit(1)

    print("🤖 正在启动 Telegram 建店机器人服务...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    msg_filter = filters.TEXT & (~filters.COMMAND)

    app.add_handler(MessageHandler(msg_filter, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.run_polling()


if __name__ == "__main__":
    main()
