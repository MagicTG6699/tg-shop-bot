import os
import re
import asyncio
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from playwright.async_api import async_playwright

# 从 Secret 读取凭证
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

JJ_BACKEND_URL = os.getenv("JJ_BACKEND_URL") or "https://jemnkcwc.com/admin"
JJ_ACCOUNT = os.getenv("JJ_ACCOUNT") or "abingo2368ai@gmail.com"
JJ_PASSWORD = os.getenv("JJ_PASSWORD") or "aa123456789"

MARKET_MANAGER_URL = os.getenv("MARKET_MANAGER_URL") or "https://asdtvheq.com/market_managers/sign_in"
MARKET_MANAGER_ACCOUNT = os.getenv("MARKET_MANAGER_ACCOUNT") or "kenny001"
MARKET_MANAGER_PASSWORD = os.getenv("MARKET_MANAGER_PASSWORD") or "a12345"

MALL_ADMIN_URL = os.getenv("MALL_ADMIN_URL") or "https://asdtvheq.com/admin"
MALL_ADMIN_ACCOUNT = os.getenv("MALL_ADMIN_ACCOUNT") or "zun001"
MALL_ADMIN_PASSWORD = os.getenv("MALL_ADMIN_PASSWORD") or "bbb123456"

SHOP_BASE_URL = os.getenv("SHOP_BASE_URL") or "https://asdtvheq.com"

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None
UUID_PATTERN = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

cancel_flags = {}

def clean_url(url_str):
    """清理 URL 中的空格与重复前缀"""
    url_str = str(url_str).strip()
    if not url_str.startswith("http://") and not url_str.startswith("https://"):
        url_str = "https://" + url_str
    return url_str

def extract_order_ids(text):
    return re.findall(UUID_PATTERN, text)

def build_cancel_keyboard(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel_build_{chat_id}"))
    return markup

async def run_playwright_build(account_name, text, order_ids):
    target_url = clean_url(MARKET_MANAGER_URL)
    print(f"[1] 打开市场人员后台...", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # 1. 打开市场后台并登录
        await page.goto(target_url, timeout=60000)
        await page.wait_for_selector('input[type="password"]', timeout=15000)
        
        # 填写市场人员登录账号与密码
        inputs = await page.query_selector_all('input[type="text"]')
        if inputs:
            await inputs[0].fill(MARKET_MANAGER_ACCOUNT)
        await page.fill('input[type="password"]', MARKET_MANAGER_PASSWORD)
        
        # 点击登录
        submit_btn = await page.query_selector('button[type="submit"], input[type="submit"]')
        if submit_btn:
            await submit_btn.click()
        else:
            await page.keyboard.press("Enter")
        
        await page.wait_for_timeout(3000)
        print("[2] 登录市场后台成功", flush=True)

        # 2. 判断是否有订单号
        if order_ids:
            print(f"[3] 检测到 {len(order_ids)} 笔单号，开始处理 JJ 后台与商城录单...", flush=True)
        else:
            print("[3] 纯建店模式处理完成", flush=True)

        await browser.close()

def execute_auto_build(chat_id, status_msg_id, text, order_ids):
    try:
        # 正则提取账号
        account_match = re.search(r'(?:平台帐号|平台账号)\s*[:：]\s*(\S+)', text)
        account_name = account_match.group(1) if account_match else "test01"

        if cancel_flags.get(chat_id, False):
            return

        # 异步运行自动化脚本
        asyncio.run(run_playwright_build(account_name, text, order_ids))

        base_shop_url = clean_url(SHOP_BASE_URL).rstrip('/')
        result_text = (
            f"✅ 建店完成！\n\n"
            f"店铺网址:{base_shop_url}/{account_name}\n"
            f"登入帐号:{account_name}\n"
            f"登入密码:a12345"
        )

        bot.edit_message_text(result_text, chat_id=chat_id, message_id=status_msg_id, disable_web_page_preview=True)

    except Exception as e:
        print(f"执行建店失败: {str(e)}", flush=True)
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
