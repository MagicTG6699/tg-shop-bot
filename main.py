import os
import re
import asyncio
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from playwright.async_api import async_playwright

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

JJ_BACKEND_URL = os.getenv("JJ_BACKEND_URL")
JJ_ACCOUNT = os.getenv("JJ_ACCOUNT")
JJ_PASSWORD = os.getenv("JJ_PASSWORD")

MARKET_MANAGER_URL = os.getenv("MARKET_MANAGER_URL")
MARKET_MANAGER_ACCOUNT = os.getenv("MARKET_MANAGER_ACCOUNT")
MARKET_MANAGER_PASSWORD = os.getenv("MARKET_MANAGER_PASSWORD")

MALL_ADMIN_URL = os.getenv("MALL_ADMIN_URL")
MALL_ADMIN_ACCOUNT = os.getenv("MALL_ADMIN_ACCOUNT")
MALL_ADMIN_PASSWORD = os.getenv("MALL_ADMIN_PASSWORD")

SHOP_BASE_URL = os.getenv("SHOP_BASE_URL")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None
UUID_PATTERN = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

cancel_flags = {}

def extract_order_ids(text):
    return re.findall(UUID_PATTERN, text)

def build_cancel_keyboard(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel_build_{chat_id}"))
    return markup

async def run_playwright_build(account_name, text, order_ids):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # 市场人员后台建店
        market_url = MARKET_MANAGER_URL if MARKET_MANAGER_URL else "https://asdtvheq.com/market_managers/sign_in"
        print(f"[1] 打开市场人员后台: {market_url}", flush=True)
        await page.goto(market_url)
        
        # 登录
        await page.fill('input[type="text"], input[name*="account"], input[name*="user"]', MARKET_MANAGER_ACCOUNT or "kenny001")
        await page.fill('input[type="password"]', MARKET_MANAGER_PASSWORD or "a12345")
        await page.click('button[type="submit"], input[type="submit"]')
        await page.wait_for_timeout(2000)

        # 判断是否包含单号（带有单号才去查 JJ 后台）
        if order_ids:
            print(f"[2] 检测到 {len(order_ids)} 笔订单号，开始在 JJ 后台反查...", flush=True)
        else:
            print("[2] 纯建店模式：无需反查 JJ 后台", flush=True)

        await browser.close()

def execute_auto_build(chat_id, status_msg_id, text, order_ids):
    try:
        # 兼容处理“平台帐号”、“平台账号”，以及冒号左右的任意空格
        account_match = re.search(r'(?:平台帐号|平台账号)\s*[:：]\s*(\S+)', text)
        account_name = account_match.group(1) if account_match else "ANANU"
        
        if cancel_flags.get(chat_id, False):
            return

        asyncio.run(run_playwright_build(account_name, text, order_ids))

        base_shop_url = (SHOP_BASE_URL if SHOP_BASE_URL else "https://asdtvheq.com").rstrip('/')
        result_text = (
            f"✅ 建店完成！\n\n"
            f"店铺网址:{base_shop_url}/{account_name}\n"
            f"登入帐号:{account_name}\n"
            f"登入密码:a12345"
        )
        
        bot.edit_message_text(result_text, chat_id=chat_id, message_id=status_msg_id, disable_web_page_preview=True)

    except Exception as e:
        print(f"建店异常: {str(e)}", flush=True)
        bot.edit_message_text(f"❌ 建店失败: {str(e)}", chat_id=chat_id, message_id=status_msg_id)

@bot.message_handler(func=lambda msg: True)
def handle_message(message):
    text = message.text
    chat_id = message.chat.id
    order_ids = extract_order_ids(text)
    
    print(f"收到消息: {text}", flush=True)

    if len(order_ids) > 3:
        bot.reply_to(message, "❌ 创建失败：仅能制作三笔内单笔！")
        return

    # 模糊匹配关键字
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
