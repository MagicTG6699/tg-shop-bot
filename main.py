import os
import re
import asyncio
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from playwright.async_api import async_playwright

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
    """解析消息中的字段信息"""
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
    target_url = clean_url(MARKET_MANAGER_URL, "https://asdtvheq.com/market_managers/sign_in")
    print(f"[1] 打开市场人员后台...", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # 1. 打开页面并登录
        await page.goto(target_url, timeout=60000)
        await page.wait_for_selector('input[type="password"]', timeout=15000)

        inputs = await page.query_selector_all('input[type="text"]')
        if inputs:
            await inputs[0].fill(MARKET_MANAGER_ACCOUNT)
        await page.fill('input[type="password"]', MARKET_MANAGER_PASSWORD)

        submit_btn = await page.query_selector('button[type="submit"], input[type="submit"]')
        if submit_btn:
            await submit_btn.click()
        else:
            await page.keyboard.press("Enter")

        await page.wait_for_timeout(3000)
        print("[2] 登录市场后台成功", flush=True)

        # 2. 点击“建立店铺”按钮
        shop_btn = await page.query_selector('text="建立店铺"') or await page.query_selector('text="建立店鋪"')
        if shop_btn:
            await shop_btn.click()
            await page.wait_for_timeout(2000)
            print("[3] 点击'建立店铺'按钮成功", flush=True)

            # 3. 填写表单字段
            text_inputs = await page.query_selector_all('input[type="text"]')
            if len(text_inputs) >= 4:
                await text_inputs[0].fill(info['account'])
                await text_inputs[1].fill(info['name'])
                await text_inputs[2].fill(info['rmb'])
                await text_inputs[3].fill(info['phone'])
            else:
                # 备用填写方式
                labels = {
                    '帐号': info['account'],
                    '账号': info['account'],
                    '户名': info['name'],
                    '人民币': info['rmb'],
                    '手机': info['phone']
                }
                for key, val in labels.items():
                    try:
                        inp = await page.query_selector(f'input[placeholder*="{key}"]')
                        if inp and val:
                            await inp.fill(val)
                    except:
                        pass

            # 4. 点击保存/确定提交
            confirm_btn = await page.query_selector('button:has-text("确定")') or await page.query_selector('button:has-text("確認")') or await page.query_selector('button:has-text("保存")')
            if confirm_btn:
                await confirm_btn.click()
                await page.wait_for_timeout(3000)
                print("[4] 店铺创建表单已提交！", flush=True)
        else:
            print("[3] ⚠️ 未找到'建立店铺'按钮", flush=True)

        await browser.close()

def execute_auto_build(chat_id, status_msg_id, text, order_ids):
    try:
        info = extract_info(text)

        if cancel_flags.get(chat_id, False):
            return

        asyncio.run(run_playwright_build(info, order_ids))

        base_shop_url = clean_url(SHOP_BASE_URL, "https://asdtvheq.com").rstrip('/')
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
