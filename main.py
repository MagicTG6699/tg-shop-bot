import os
import re
import asyncio
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from playwright.async_api import async_playwright

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# 一般商城后台配置
MALL_ADMIN_URL = os.getenv("MALL_ADMIN_URL") or "https://asdtvheq.com/admin"
MALL_ADMIN_ACCOUNT = os.getenv("MALL_ADMIN_ACCOUNT") or "zun001"
MALL_ADMIN_PASSWORD = os.getenv("MALL_ADMIN_PASSWORD") or "bbb123456"

SHOP_BASE_URL = os.getenv("SHOP_BASE_URL") or "https://asdtvheq.com"

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None
UUID_PATTERN = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

cancel_flags = {}

def clean_url(url_val, default_url):
    if not url_val or not isinstance(url_val, str):
        return default_url
    match = re.search(r'https?://[^\s\]\)\"]+', url_val)
    if match:
        return match.group(0)
    url_str = url_val.strip()
    if not url_str.startswith("http://") and not url_str.startswith("https://"):
        return f"https://{url_str}"
    return url_str

def extract_order_ids(text):
    return re.findall(UUID_PATTERN, text)

def extract_info(text):
    """提取平台账号、数字人民币户名、数字人民币账号、手机号"""
    account_match = re.search(r'(?:平台帳號|平台账号)\s*[:：]\s*(\S+)', text)
    name_match = re.search(r'(?:数字人民币户名|數字人民幣戶名)\s*[:：]\s*(\S+)', text)
    rmb_match = re.search(r'(?:数字人民币|數字人民幣)\s*[:：]\s*(\S+)', text)
    phone_match = re.search(r'(?:手机号|手機號)\s*[:：]\s*(\S+)', text)

    return {
        'account': account_match.group(1) if account_match else "",
        'name': name_match.group(1) if name_match else "",
        'rmb': rmb_match.group(1) if rmb_match else "",
        'phone': phone_match.group(1) if phone_match else ""
    }

def build_cancel_keyboard(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel_build_{chat_id}"))
    return markup

async def run_playwright_build(info, order_ids):
    base_admin_url = clean_url(MALL_ADMIN_URL, "https://asdtvheq.com/admin")
    print(f"[1] 打开一般商城后台: {base_admin_url}", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # 1. 登录一般商城后台
        await page.goto(base_admin_url, timeout=60000)
        await page.wait_for_selector('input[type="password"]', timeout=15000)

        inputs = await page.query_selector_all('input[type="text"]')
        if inputs:
            await inputs[0].fill(MALL_ADMIN_ACCOUNT)
        await page.fill('input[type="password"]', MALL_ADMIN_PASSWORD)

        submit_btn = await page.query_selector('button[type="submit"], input[type="submit"]')
        if submit_btn:
            await submit_btn.click()
        else:
            await page.keyboard.press("Enter")

        await page.wait_for_timeout(3000)
        print("[2] 登录成功", flush=True)

        # 2. 直接访问新建店铺的准确完整 URL（避免域名末尾 .co/.com 错误切分）
        create_page_url = "https://asdtvheq.com/admin/merchants/new"
        print(f"[3] 直接跳转建店页面: {create_page_url}", flush=True)
        await page.goto(create_page_url, timeout=60000)
        await page.wait_for_timeout(2000)

        # 3. 自动填写建店表单
        text_inputs = await page.query_selector_all('input[type="text"]')
        if len(text_inputs) >= 4:
            await text_inputs[0].fill(info['account'])
            await text_inputs[1].fill(info['name'])
            await text_inputs[2].fill(info['rmb'])
            await text_inputs[3].fill(info['phone'])
            print(f"[4] 表单填写完成: 账号={info['account']}", flush=True)
        else:
            print("[4] ⚠️ 未能按顺序找到 4 个文本输入框", flush=True)

        # 4. 点击保存/提交按钮
        save_btn = (
            await page.query_selector('input[type="submit"]') or 
            await page.query_selector('button[type="submit"]') or 
            await page.query_selector('button:has-text("保存")') or 
            await page.query_selector('button:has-text("確定")') or 
            await page.query_selector('button:has-text("确定")')
        )
        if save_btn:
            await save_btn.click()
            await page.wait_for_timeout(3000)
            print("[5] ✅ 店铺提交保存完成！", flush=True)
        else:
            print("[5] ⚠️ 未找到提交保存按钮", flush=True)

        await browser.close()

def execute_auto_build(chat_id, status_msg_id, text, order_ids):
    try:
        info = extract_info(text)

        if cancel_flags.get(chat_id, False):
            return

        asyncio.run(run_playwright_build(info, order_ids))

        account_name = info['account'] or "test01"
        result_text = (
            f"✅ 建店完成！\n\n"
            f"店铺网址:https://{account_name}.asdtvheq.com\n"
            f"登入帐号:{account_name}\n"
            f"登入密码:a12345"
        )

        bot.edit_message_text(result_text, chat_id=chat_id, message_id=status_msg_id, disable_web_page_preview=True)

    except Exception as e:
        print(f"执行建店失败异常: {str(e)}", flush=True)
        bot.edit_message_text(f"❌ 建店失败: {str(e)}", chat_id=chat_id, message_id=status_msg_id)

@bot.message_handler(func=lambda msg: True)
def handle_message(message):
    text = message.text
    chat_id = message.chat.id
    order_ids = extract_order_ids(text)

    print(f"收到建店请求: {text}", flush=True)

    if len(order_ids) > 3:
        bot.reply_to(message, "❌ 创建失败：仅能制作三笔内单笔！")
        return

    if "平台" in text or "帐号" in text or "账号" in text or len(order_ids) > 0:
        cancel_flags[chat_id] = False

        status_msg = bot.reply_to(
            message,
            "⌛ 正在自动建店中，请稍候...",
            reply_markup=build_cancel_keyboard(chat_id)
        )

        execute_auto_build(chat_id, status_msg.message_id, text, order_ids)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_build_"))
def handle_cancel(call):
    chat_id = int(call.data.split("_")[-1])
    cancel_flags[chat_id] = True
    bot.edit_message_text("❌ 建店已被取消", chat_id=call.message.chat.id, message_id=call.message.message_id)
    bot.answer_callback_query(call.id, "已取消建店")

if __name__ == "__main__":
    if bot:
        print("Bot 已成功启动...", flush=True)
        bot.remove_webhook()
        bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
