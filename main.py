import os
import re
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==========================================================
# 环境变量安全读取（读取 GitHub Secrets）
# ==========================================================
# 1. Telegram Bot Token
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# 2. JJ 订单后台凭证与网址
JJ_BACKEND_URL = os.getenv("JJ_BACKEND_URL")
JJ_ACCOUNT = os.getenv("JJ_ACCOUNT")
JJ_PASSWORD = os.getenv("JJ_PASSWORD")
JJ_2FA_SECRET = os.getenv("JJ_2FA_SECRET")

# 3. 市场人员后台凭证与网址 (单笔/三笔建店用)
MARKET_MANAGER_URL = os.getenv("MARKET_MANAGER_URL")
MARKET_MANAGER_ACCOUNT = os.getenv("MARKET_MANAGER_ACCOUNT")
MARKET_MANAGER_PASSWORD = os.getenv("MARKET_MANAGER_PASSWORD")

# 4. 商城后台凭证与网址 (充值/提现/额度管理用)
MALL_ADMIN_URL = os.getenv("MALL_ADMIN_URL")
MALL_ADMIN_ACCOUNT = os.getenv("MALL_ADMIN_ACCOUNT")
MALL_ADMIN_PASSWORD = os.getenv("MALL_ADMIN_PASSWORD")

# 5. 商城前端通用基础网址
SHOP_BASE_URL = os.getenv("SHOP_BASE_URL")

# 初始化 Bot 实例
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

# UUID/GUID 匹配正则（用于提取订单号）
UUID_PATTERN = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

# 全局字典：用于记录用户是否点击了“取消建店”
cancel_flags = {}

def extract_order_ids(text):
    """提取消息中的 GUID 订单号列表"""
    return re.findall(UUID_PATTERN, text)

def parse_user_input(text):
    """解析传入的基本建店资讯"""
    lines = text.strip().split('\n')
    info = {}
    for line in lines:
        if ":" in line or "：" in line:
            parts = re.split(r'[:：]', line, 1)
            info[parts[0].strip()] = parts[1].strip()
    return info

def build_cancel_keyboard(chat_id):
    """生成带取消按钮的内联键盘"""
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel_build_{chat_id}"))
    return markup

@bot.message_handler(func=lambda msg: True)
def handle_message(message):
    text = message.text
    chat_id = message.chat.id
    order_ids = extract_order_ids(text)
    
    # 规则拦截：如果订单号超过 3 笔，直接拒绝并报错提醒
    if len(order_ids) > 3:
        bot.reply_to(message, "❌ 创建失败：仅能制作三笔内单笔！")
        return

    # 规则处理：1 ~ 3 笔单号正常处理
    if 1 <= len(order_ids) <= 3:
        # 重置取消标记
        cancel_flags[chat_id] = False
        
        # 1. 立即回复等待提示，附带内联取消按钮
        status_msg = bot.reply_to(
            message, 
            "⌛ 正在自动建店中，请稍候...", 
            reply_markup=build_cancel_keyboard(chat_id)
        )
        
        # 2. 触发自动化主流程
        execute_auto_build(chat_id, status_msg.message_id, text, order_ids)

def execute_auto_build(chat_id, status_msg_id, text, order_ids):
    """
    建店核心逻辑（Playwright / Selenium 自动化）
    需执行的操作包含：
    1. JJ 后台反查：必须先点击右上角【🔒 锁头图标】解暗锁，并设置【一年前历史】
    2. 分流判定：
       - 【出货管理】搜平台/其他订单号 -> 匹配商城【商户充值管理】
       - 【拼多多订单管理】搜订单号 -> 匹配商城【商户提现管理】
    3. 登录市场人员后台建店（导入 60 商品，移除占位卡）
    4. 商城后台录入对应充值/提现记录
    5. 完成后回传格式化消息
    """
    try:
        info = parse_user_input(text)
        account_name = info.get("平台帐号", "ANANU")
        
        # 中途检查是否取消
        if cancel_flags.get(chat_id, False):
            return

        # TODO: 在此处调用网页自动化控制脚本
        # 此处使用的所有凭证变量均为顶部的环境变量 (如 JJ_ACCOUNT, MARKET_MANAGER_URL 等)
        
        # 建店成功后，更新回传格式
        base_shop_url = SHOP_BASE_URL if SHOP_BASE_URL else "https://asdtvheq.com"
        result_text = (
            f"店铺网址: {base_shop_url}/{account_name}\n"
            f"登入帐号: {account_name}\n"
            f"登入密码: a12345"
        )
        
        bot.edit_message_text(
            result_text, 
            chat_id=chat_id, 
            message_id=status_msg_id
        )

    except Exception as e:
        bot.edit_message_text(
            f"❌ 建店失败: {str(e)}", 
            chat_id=chat_id, 
            message_id=status_msg_id
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_build_"))
def handle_cancel(call):
    """处理用户点击【❌ 取消建店】按钮"""
    chat_id = int(call.data.split("_")[-1])
    cancel_flags[chat_id] = True
    
    bot.edit_message_text(
        "❌ 建店已被取消", 
        chat_id=call.message.chat.id, 
        message_id=call.message.message_id
    )
    bot.answer_callback_query(call.id, "已取消建店操作")

if __name__ == "__main__":
    if bot:
        print("Bot 已成功启动且正常运行中...")
        bot.infinity_polling()
    else:
        print("错误：无法读取 TELEGRAM_BOT_TOKEN 环境变量，请检查配置！")
