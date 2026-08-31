import os
import re
import asyncio
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from playwright.async_api import async_playwright

# 读取环境变量
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

JJ_BACKEND_URL = os.getenv("JJ_BACKEND_URL")
JJ_ACCOUNT = os.getenv("JJ_ACCOUNT")
JJ_PASSWORD = os.getenv("JJ_PASSWORD")

# 获取网址，若未配置或为空则自动启用预设网址
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

def format_url(url):
    """确保 URL 包含正确的 http/https 前缀"""
    url = str(url).strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return f"https://{url}"
    return url

def extract_order_ids(text):
    return re.findall(UUID_PATTERN, text)

def build_cancel_keyboard(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel_build_{chat_id}"))
    return markup

async def run_playwright_build(account_name, text, order_ids):
    target_url = format_url(MARKET_MANAGER_URL)
    print(f"[1] 准备打开市场人员后台 URL: {target_url}", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # 打开市场人员后台
        await page.goto(target_url, wait_until="networkidle")
        print("[1] 成功载入页面，开始填入登录凭证...", flush=True)

        # 输入账号与密码登录
        await page.fill('input[type="text"], input[name*="account"], input[name*="user"]', MARKET_MANAGER_ACCOUNT)
        await page.fill('input[type="password"]', MARKET_MANAGER_PASSWORD)
        await page.click('button[type="submit"], input[type="submit"]')
        await page.wait_for_timeout(2000)

        if order_ids:
            print(f"[2] 检测到 {len(order_ids)} 笔订单号，准备进行 JJ 后台反查...", flush=True)
        else:
            print("[2] 纯建店模式：跳过 JJ 后台反查", flush=True)

        await browser.close()

def execute_auto_build(chat_id, status_msg_id, text, order_ids):
    try:
        # 解析输入的平台账号（支持格式：平台帐号 : ANANU / 平台账号: ANANU / test01）
        account_match = re.search(r'(?:平台帐号|平台账号)\s*[:：]\s*(\S+)', text)
        account_name = account_match.group(1) if account_match else "ANANU"

        if cancel_flags.get(chat_id, False):
            return

        # 执行网页自动化
        asyncio.run(run_playwright_build(account_name, text, order_ids))

        base_shop_url = format_url(SHOP_BASE_URL).rstrip('/')
        result_text = (
            f"✅ 建店完成！\n\n"
            f"店铺网址:{base_shop_url}/{account_name}\n"
            f"登入帐号:{account_name}\n"
            f"登入密码:a12345"
        )

        bot.edit_message_text(result_text, chat_id=chat_id, message_id=status_msg_id, disable_web_page_preview=True)

    except Exception as e:
        print(f"建店失败异常: {str(e)}", flush=True)
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
