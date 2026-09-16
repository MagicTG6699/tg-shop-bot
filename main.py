import os
import sys
import asyncio
import re
import html
import random
import traceback
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
# v34：JJ 查询会话复用 + 暗锁/日期范围验证 + 查询异常自动重登恢复
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


class JJOrderNotFound(Exception):
    """JJ 出货管理中真正没有找到订单；只有此异常允许分流到拼多多。"""
    pass


def _debug_log(message):
    """内部诊断日志已关闭。"""
    return

# 全局任务字典
ACTIVE_TASKS = {}


class _ReusableBrowserSession:
    """同一单笔任务内复用 Playwright 登录会话，减少重复启动浏览器和重复登录。"""
    def __init__(self, use_totp=False):
        self.use_totp = use_totp
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def start(self, url, username, password, task_id=None):
        if self.page is not None and not self.page.is_closed():
            if task_id and task_id in ACTIVE_TASKS:
                ACTIVE_TASKS[task_id]["page"] = self.page
            return self.page
        if not url or not username or not password:
            raise Exception("后台登录配置不完整。")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"]
        )
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        self.page.set_default_timeout(20000)
        if task_id and task_id in ACTIVE_TASKS:
            ACTIVE_TASKS[task_id]["page"] = self.page
        await _login_generic(self.page, url, username, password, use_totp=self.use_totp)
        return self.page

    async def close(self):
        try:
            if self.context is not None:
                await self.context.close()
        except Exception:
            pass
        try:
            if self.browser is not None:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright is not None:
                await self.playwright.stop()
        except Exception:
            pass
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    async def reset(self, url, username, password, task_id=None):
        """重置当前浏览器会话并重新登录。只用于查询阶段的恢复，避免坏页面状态影响下一笔。"""
        await self.close()
        return await self.start(url, username, password, task_id=task_id)

# 【建店专用排队锁】：同时只允许 1 个建店任务在后台运行，后续建店请求自动排队
BUILD_SHOP_SEMAPHORE = asyncio.Semaphore(1)

# 商城界面选项（与后台对应）
SKIN_OPTIONS = {
    "jisumeishang": "极速微商",
    "qimiao": "七喵",
    "qiyue": "柒月",
    "yinnierlai": "音你而来"
}


def _extract_order_numbers(text: str):
    """提取消息里的 JJ 订单号，最多 5 笔。

    触发方式：
    1. 单笔 / 單筆 : 订单号（可同一行放多个，用逗号、空格、分号等分隔）
    2. 订单号 / 訂單號 / 平台订单号等字段
    3. 消息中直接出现 UUID 格式订单号
    """
    clean = re.sub(r'mailto:', '', text or '', flags=re.IGNORECASE)
    clean = re.sub(r'https?://[^\s]+', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<[^>]+>', '', clean)

    found = []

    def add(value):
        value = (value or '').strip().strip('`<>[](){}"\'“”‘’')
        if not value:
            return
        # 优先从一段文字中抓 UUID；如果没有 UUID，再接受单个常规订单号 token。
        uuids = re.findall(
            r'(?i)(?<![0-9a-f])'
            r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
            r'(?![0-9a-f])', value
        )
        if uuids:
            for x in uuids:
                if x not in found:
                    found.append(x)
            return

        # 同一字段支持多个订单号，避免把整段说明文字当成订单号。
        parts = re.split(r'[\s,，;；|]+', value)
        for part in parts:
            part = part.strip().strip('`<>[](){}"\'“”‘’')
            if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{7,127}', part):
                if part not in found:
                    found.append(part)

    # 带标签的订单号：最可靠。
    for line in clean.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(
            r'(?i)^(?:单笔|單筆|订单号|訂單號|订单号码|訂單號碼|平台订单号|平台訂單號|平台订单|平台訂單)'
            r'\s*[:：=]\s*(.+?)\s*$', line
        )
        if m:
            add(m.group(1))

    # 直接出现 UUID 也视为订单号，即使没有“单笔/订单号”文字。
    uuid_hits = re.findall(
        r'(?i)(?<![0-9a-f])'
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
        r'(?![0-9a-f])', clean
    )
    for x in uuid_hits:
        if x not in found:
            found.append(x)

    return found


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

    # 单笔订单号：
    # 不再要求一定出现“单笔”。只要检测到订单号，就自动走单笔商城。
    order_numbers = _extract_order_numbers(clean_text)
    if len(order_numbers) > 5:
        errors.append("• 已超过单笔最大笔数")
    elif order_numbers:
        info["single_order_nos"] = order_numbers
        # 保留旧字段，兼容其他旧流程。
        info["single_order_no"] = order_numbers[0]
    elif re.search(r'(?im)^\s*(?:单笔|單筆)\s*[:：=]\s*$', clean_text):
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
                merchant_username = page.locator("#merchant_username").first
                await merchant_username.wait_for(state="visible", timeout=20000)

                await merchant_username.fill(current_account)
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
                branch_name_input = page.locator("#merchant_bank_accounts_attributes_0_bank_branch_name, #merchant_bank_accounts_attributes_0_branch_name, input[id$='_bank_branch_name'], input[id$='_branch_name']").first
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


def _extract_explicit_real_name(value: str) -> str:
    """只读取订单资料里明确标示的“实名/實名”。没有显示实名就返回空。"""
    text = _clean_text_value(value)
    if not text:
        return ""

    m = re.search(r"(?:实名|實名)\s*[:：]\s*([^|;；,，\n\r]+)", text, re.I)
    if m:
        return _clean_text_value(m.group(1))
    return ""


def _safe_real_name_from_order(value: str) -> str:
    """订单没有明确显示实名，或实名不是有效真人姓名时，一律使用管理员代收。"""
    explicit = _extract_explicit_real_name(value)
    return _safe_manager_name(explicit)


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
    """解析 JJ 时间；兼容完整日期、中文日期，以及截图中的“01月19日 08:30”。

    JJ 当前结果表的第一列实际显示的是“提交时间”，例如：
    01月19日 08:30\n    东京都东京\n    页面没有显示年份，因此这里使用当前北京时间年份，并在日期落在未来时回退一年。
    这样不会因为“建立时间”列不存在而把一个已经找到的订单误判为查询失败。
    """
    value = _clean_text_value(value)
    if not value:
        return None

    # 先从整段文字里抓出日期时间，避免“东京都东京”等地点文字干扰。
    m = re.search(
        r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})(?:日)?[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?",
        value,
    )
    if m:
        try:
            return datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
            )
        except ValueError:
            pass

    # JJ 截图目前实际格式：01月19日 08:30（没有年份）。
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", value)
    if m:
        try:
            from datetime import timezone
            tz8 = timezone(timedelta(hours=8))
            now = datetime.now(tz8).replace(tzinfo=None)
            candidate = datetime(
                now.year, int(m.group(1)), int(m.group(2)),
                int(m.group(3)), int(m.group(4)), int(m.group(5) or 0)
            )
            # 如果无年份的日期明显落在当前时间之后，按上一年处理。
            if candidate > now + timedelta(days=1):
                candidate = candidate.replace(year=candidate.year - 1)
            return candidate
        except ValueError:
            pass

    # 最后兼容普通纯日期字符串。
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
    # 单笔商城实际商户列表是 /market_manager/merchants，搜索框实际为 q_username_eq。
    await page.goto(f"{SINGLE_ADMIN_ROOT}/market_manager/merchants", wait_until="domcontentloaded")

    # 优先使用截图/DevTools 已确认的真实字段，再做旧版兼容。
    search_input = await _first_visible(page, [
        "#q_username_eq",
        "input[name='q[username_eq]']",
        "#q_username",
        "input[name='q[username]']",
        "input[name*='account']",
        "#search_account",
        "input[type='search']",
        "input[type='text']",
    ], timeout=8000)
    if not search_input:
        raise Exception(f"单笔商城找不到商户搜索框；当前地址：{page.url}；标题：{await page.title()}")
    await search_input.fill(account)

    search_btn = await _first_visible(page, [
        "button:has-text('搜尋')",
        "button:has-text('搜索')",
        "input[type='submit']",
        ".btn-primary",
    ], timeout=3000)
    if search_btn:
        await search_btn.click()
    else:
        await search_input.press("Enter")

    await page.wait_for_timeout(250)
    rows = page.locator("tbody tr")
    for i in range(await rows.count()):
        row = rows.nth(i)
        try:
            txt = _clean_text_value(await row.inner_text())
            if account.lower() in txt.lower():
                return row
        except Exception:
            pass
    raise Exception(f"单笔商城找不到商户【{account}】")


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
                await page.goto(f"{SINGLE_ADMIN_ROOT}/market_manager/merchants/new", wait_until="domcontentloaded")
                merchant_username = page.locator("#merchant_username").first
                try:
                    await merchant_username.wait_for(state="visible", timeout=20000)
                except Exception:
                    current_url = page.url
                    title = await page.title()
                    body_preview = ""
                    try:
                        body_preview = (await page.locator("body").inner_text())[:500].replace("\n", " ")
                    except Exception:
                        pass
                    raise Exception(f"单笔商城未进入【新增商户】页面；当前地址：{current_url}；标题：{title}；页面内容：{body_preview}")
                await merchant_username.fill(current_account)

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
                                await sprite.select_option(value="jj")
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
                    "#merchant_bank_accounts_attributes_0_bank_branch_name, #merchant_bank_accounts_attributes_0_branch_name, input[id$='_bank_branch_name'], input[id$='_branch_name']"
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

            # 到这里为止，单笔商城建店流程已经完成。
            # JJ 查询与充值由 Worker 在建店成功之后独立执行，避免 JJ 后台异常影响建店。
            msg_text = (
                "✅ <b>单笔商城建店完成！</b>\n\n"
                f"店铺网址 : <code>{html.escape(shop_url)}</code>\n"
                f"登入帐号 : <code>{html.escape(final_account)}</code>\n"
                "登入密码 : <code>a12345</code>"
            )
            return msg_text, final_account

        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def _jj_open_outbound(page):
    """打开 JJ 的【出货管理】实际子页面。

    注意：截图确认左侧有两层同名文字：
      进货/出货类父菜单（带展开箭头）
      └─ 出货管理（真正进入 guest_payment_orders 的子菜单）

    不能直接点击第一个 a:has-text('出货管理')，否则只会展开/收起父菜单，
    后面就会因为仍停留在首页而找不到订单号输入框。
    """
    _debug_log(f"[出货] 开始打开出货管理；当前 URL={page.url}")

    # 已经在正确页面，直接继续。
    if "guest_payment_orders" in (page.url or "").lower():
        _debug_log(f"[出货] 当前已经是 guest_payment_orders 页面；URL={page.url}")
        return

    # ① 先找到“出货管理”的父菜单并确保它展开。
    parent_selectors = [
        "li.treeview:has(> a span:text-is('出货管理')) > a",
        "li.treeview:has(> a span:text-is('出貨管理')) > a",
        "li.treeview:has(> a:has-text('出货管理')) > a",
        "li.treeview:has(> a:has-text('出貨管理')) > a",
    ]
    parent_opened = False
    for selector in parent_selectors:
        loc = page.locator(selector).first
        try:
            if not await loc.count() or not await loc.is_visible():
                continue
            parent_li = loc.locator("..").first
            cls = (await parent_li.get_attribute("class") or "").lower()
            aria = await loc.get_attribute("aria-expanded")
            child_visible = False
            for child_selector in [
                "ul.treeview-menu li a:has-text('出货管理')",
                "ul.treeview-menu li a:has-text('出貨管理')",
            ]:
                child = parent_li.locator(child_selector).first
                if await child.count() and await child.is_visible():
                    child_visible = True
                    break
            if child_visible or "menu-open" in cls or "active" in cls or aria == "true":
                parent_opened = True
                _debug_log(f"[出货] 父菜单已展开；class={cls!r}, aria-expanded={aria!r}")
                break
            await loc.click(force=True)
            await page.wait_for_timeout(250)
            parent_opened = True
            _debug_log(f"[出货] 已点击父菜单展开【出货管理】")
            break
        except Exception as e:
            _debug_log(f"[出货] 父菜单候选失败 {selector}: {e!r}")

    # ② 只点击父菜单下面的“真正子菜单”，避免误点同名父菜单。
    child_selectors = [
        "li.treeview > ul.treeview-menu li a:has(> span:text-is('出货管理'))",
        "li.treeview > ul.treeview-menu li a:has(> span:text-is('出貨管理'))",
        "li.treeview > ul.treeview-menu li a:has-text('出货管理')",
        "li.treeview > ul.treeview-menu li a:has-text('出貨管理')",
    ]
    for selector in child_selectors:
        loc = page.locator(selector).first
        try:
            if await loc.count() and await loc.is_visible():
                href = await loc.get_attribute("href")
                _debug_log(f"[出货] 找到真正的【出货管理】子菜单，href={href!r}")
                await loc.click()
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(200)
                _debug_log(f"[出货] 已进入出货管理；URL={page.url}")
                if "guest_payment_orders" in (page.url or "").lower():
                    return
        except Exception as e:
            _debug_log(f"[出货] 点击子菜单失败 {selector}: {e!r}")

    # ③ href 精确兜底：只允许 guest_payment_orders，不再点击任意同名父菜单。
    for selector in [
        "a[href*='guest_payment_orders']",
        "a[href*='/guest_payment_orders']",
    ]:
        loc = page.locator(selector).first
        try:
            if await loc.count() and await loc.is_visible():
                href = await loc.get_attribute("href")
                _debug_log(f"[出货] 通过 guest_payment_orders 链接进入；href={href!r}")
                await loc.click()
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(200)
                _debug_log(f"[出货] 进入出货管理；URL={page.url}")
                return
        except Exception as e:
            _debug_log(f"[出货] guest_payment_orders 兜底失败: {e!r}")

    # ④ 最后直接 URL 兜底。
    jj_base = JJ_ADMIN_URL.rstrip('/')
    if re.search(r'/admin$', jj_base, re.I):
        fallback_url = jj_base + "/guest_payment_orders"
    elif re.search(r'/sign_in$', jj_base, re.I):
        fallback_url = re.sub(r'/sign_in$', '', jj_base, flags=re.I) + "/guest_payment_orders"
    else:
        fallback_url = jj_base + "/admin/guest_payment_orders"
    try:
        _debug_log(f"[出货] 菜单进入失败，直接打开兜底 URL={fallback_url}")
        await page.goto(fallback_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(200)
    except Exception as e:
        raise Exception(f"JJ 后台无法进入【出货管理】页面：{e!r}")

    if "guest_payment_orders" not in (page.url or "").lower():
        raise Exception(f"JJ 后台无法进入【出货管理】页面；当前URL={page.url}")


async def _jj_unlock_search_range(page):
    """针对 JJ 当前页面真正执行“暗锁解锁”。

    重点修正：JJ 的锁头在不同页面/不同前端版本中，不一定会把
    ``fa-lock`` 直接替换成 ``fa-unlock``。因此不能只靠图标 class 判断。
    本函数会同时检查：
      1) 锁头/容器是否出现 unlock 状态；
      2) 日期输入框的 readonly / disabled 状态是否解除；
      3) 点击后是否触发了 DOM 属性变化；
      4) 必要时分别点击图标、容器，并使用 JS click 兜底。

    真正能不能搜到一年前的订单，最后仍由 _jj_set_one_year_date() 验证。
    """
    lock_containers = [
        ".toggle-order-search-days-btn-placeholder",
        ".toggle-search-days-btn-placeholder",
        ".toggle-order-search-days-btn",
    ]
    lock_icons = [
        "i.fa-lock.lock-btn",
        "i.fas.fa-lock.lock-btn",
        ".toggle-order-search-days-btn-placeholder i.fa-lock",
        ".toggle-search-days-btn-placeholder i.fa-lock",
        ".toggle-order-search-days-btn i.fa-lock",
        ".lock-btn",
    ]

    date_selectors = [
        "#q_created_at_gte",
        "input[name='q[created_at_gte]']",
        "input[id*='created_at_gte']",
        "input[name*='created_at_gte']",
        "#q_created_at_lte",
        "input[name='q[created_at_lte]']",
        "input[id*='created_at_lte']",
        "input[name*='created_at_lte']",
    ]

    def norm_class(value):
        return re.sub(r"\s+", " ", (value or "").strip().lower())

    async def get_date_state():
        states = []
        for sel in date_selectors:
            try:
                loc = page.locator(sel).first
                if not await loc.count():
                    continue
                states.append({
                    "selector": sel,
                    "disabled": await loc.is_disabled(),
                    "readonly": bool(await loc.get_attribute("readonly") is not None),
                    "value": await loc.input_value(),
                })
            except Exception:
                continue
        return states

    async def inspect_state():
        """返回 (is_unlocked, evidence)。不要只看 fa-unlock。"""
        # A. 明确的 unlock 图标/按钮。
        for sel in [
            ".toggle-order-search-days-btn-placeholder .fa-unlock",
            ".toggle-search-days-btn-placeholder .fa-unlock",
            ".toggle-order-search-days-btn .fa-unlock",
            "i.fa-unlock",
            ".unlock-btn",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    cls = norm_class(await loc.get_attribute("class"))
                    if "fa-lock" not in cls or "unlock" in cls:
                        return True, f"unlock selector={sel}, class={cls!r}"
            except Exception:
                pass

        # B. 检查锁容器的 class / data / aria / HTML。
        for sel in lock_containers + lock_icons:
            try:
                loc = page.locator(sel).first
                if not await loc.count():
                    continue
                cls = norm_class(await loc.get_attribute("class"))
                html_text = (await loc.evaluate("el => el.outerHTML") or "").lower()
                for attr in ["data-locked", "aria-pressed", "aria-expanded", "data-unlocked"]:
                    val = (await loc.get_attribute(attr) or "").strip().lower()
                    if attr == "data-unlocked" and val in ("true", "1", "yes"):
                        return True, f"{attr}={val}"
                    if attr == "data-locked" and val in ("false", "0", "no"):
                        return True, f"{attr}={val}"
                if "fa-unlock" in cls:
                    return True, f"class={cls!r}"
                if "fa-unlock" in html_text or "unlock-btn" in html_text:
                    return True, "outerHTML contains unlock state"
            except Exception:
                continue

        # C. 很多版本真正的“解锁”表现是日期框解除 readonly/disabled，
        # 即使图标 class 仍然叫 fa-lock，也应视为解锁成功。
        states = await get_date_state()
        usable = [x for x in states if not x["disabled"] and not x["readonly"]]
        if len(usable) >= 2:
            return True, f"date inputs editable ({len(usable)}/{len(states)})"

        return False, "仍检测到锁定状态 / 日期输入框仍不可编辑"

    # 已经解锁就不要重复点击。
    unlocked, evidence = await inspect_state()
    if unlocked:
        _debug_log(f"[JJ] 暗锁当前已可用：{evidence}")
        return True

    before_date_state = await get_date_state()
    before_signature = "|".join(
        f"{x['disabled']}:{x['readonly']}:{x['value']}" for x in before_date_state
    )

    # 先尝试最精确的图标，再尝试 placeholder / 容器。
    click_candidates = lock_icons + lock_containers
    clicked = False
    clicked_selector = ""
    for sel in click_candidates:
        try:
            loc = page.locator(sel).first
            if not await loc.count():
                continue
            # 即使 Playwright 判定不可见，也允许最后通过 JS click 触发页面事件。
            if await loc.is_visible():
                await loc.click(force=True)
                clicked = True
                clicked_selector = sel
                _debug_log(f"[JJ] 已点击暗锁：{sel}")
                break
        except Exception as e:
            _debug_log(f"[JJ] 点击暗锁失败 {sel}: {e!r}")

    # 如果正常 click 没成功，再用 JS click 触发绑定在元素上的事件。
    if not clicked:
        for sel in lock_icons + lock_containers:
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    await loc.evaluate("el => el.click()")
                    clicked = True
                    clicked_selector = sel + " [js-click]"
                    _debug_log(f"[JJ] 已使用 JS click 暗锁：{sel}")
                    break
            except Exception as e:
                _debug_log(f"[JJ] JS click 暗锁失败 {sel}: {e!r}")

    if not clicked:
        _debug_log("[JJ] 页面找不到可触发的暗锁")
        return False

    # 点击后给前端 AJAX / class / readonly 状态充分时间。
    # 先看状态变化，不要求一定出现 fa-unlock。
    for i in range(80):
        await page.wait_for_timeout(100)

        unlocked, evidence = await inspect_state()
        if unlocked:
            _debug_log(f"[JJ] 暗锁已确认可用：{evidence}；等待={(i+1)*0.1:.1f}s")
            return True

        # 检查日期框属性签名是否发生变化；如果页面没有 fa-unlock，
        # 但前端确实已经切换状态，也允许继续由日期范围验证。
        try:
            current_state = await get_date_state()
            current_signature = "|".join(
                f"{x['disabled']}:{x['readonly']}:{x['value']}" for x in current_state
            )
            if current_signature != before_signature:
                editable = [x for x in current_state if not x["disabled"] and not x["readonly"]]
                if len(editable) >= 2:
                    _debug_log(
                        f"[JJ] 暗锁触发后日期控件状态已变化并可编辑：{clicked_selector}"
                    )
                    return True
            before_signature = current_signature
        except Exception:
            pass

    _debug_log(f"[JJ] 暗锁点击后 8 秒仍未确认解锁：clicked={clicked_selector}")
    return False


async def _jj_set_one_year_date(page):
    """设置 JJ 建立日期范围为最近一年，并验证输入值确实写入成功。"""
    from datetime import timezone
    tz8 = timezone(timedelta(hours=8))
    now = datetime.now(tz8)
    start = now - timedelta(days=365)

    async def find_input(selectors):
        contexts = [page] + list(page.frames)
        for ctx in contexts:
            for selector in selectors:
                try:
                    loc = ctx.locator(selector).first
                    if await loc.count():
                        return loc
                except Exception:
                    continue
        return None

    start_input = await find_input([
        "#q_created_at_gte",
        "input[name='q[created_at_gte]']",
        "input[id*='created_at_gte']",
        "input[name*='created_at_gte']",
    ])
    end_input = await find_input([
        "#q_created_at_lte",
        "input[name='q[created_at_lte]']",
        "input[id*='created_at_lte']",
        "input[name*='created_at_lte']",
    ])

    if start_input is None or end_input is None:
        _debug_log("[JJ] 找不到建立日期范围输入框，无法验证一年范围")
        return False

    start_value = start.strftime("%Y/%m/%d %H:%M")
    end_value = now.strftime("%Y/%m/%d %H:%M")

    async def set_value(loc, value):
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

    def norm_dt(v):
        return re.sub(r"[^0-9]", "", v or "")[:12]

    await set_value(start_input, start_value)
    await set_value(end_input, end_value)

    # 给日期控件一点时间处理 input/change 事件。
    for _ in range(10):
        await page.wait_for_timeout(100)
        try:
            actual_start = await start_input.input_value()
            actual_end = await end_input.input_value()
            if norm_dt(actual_start) == norm_dt(start_value) and norm_dt(actual_end) == norm_dt(end_value):
                _debug_log(f"[JJ] 建立日期已验证：{actual_start!r} -> {actual_end!r}")
                return True
        except Exception:
            pass

    try:
        actual_start = await start_input.input_value()
        actual_end = await end_input.input_value()
    except Exception:
        actual_start, actual_end = "", ""
    _debug_log(
        f"[JJ] 建立日期验证失败：目标={start_value!r}->{end_value!r}，实际={actual_start!r}->{actual_end!r}"
    )
    return False


async def _jj_prepare_search_range(page):
    """准备 JJ 搜索范围。

    最终以“最近一年日期真的写入并被页面接受”为有效标准；
    不因为锁头 icon 没有改 class 就误判，也不在暗锁未处理时盲目搜索。
    """
    unlocked = await _jj_unlock_search_range(page)
    if not unlocked:
        _debug_log("[JJ] 第一次未确认暗锁，刷新页面后重新进入完整解锁流程")
        try:
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(800)
            unlocked = await _jj_unlock_search_range(page)
        except Exception as e:
            _debug_log(f"[JJ] 刷新后解暗锁失败：{e!r}")
    if not unlocked:
        return False

    # 真正的最终验证：日期必须接受最近一年，而不是只看锁头图标。
    date_ok = await _jj_set_one_year_date(page)
    if date_ok:
        _debug_log("[JJ] 暗锁可用 + 最近一年日期已确认写入")
        return True

    # 日期没有接受，通常代表暗锁实际没有解除或前端 AJAX 尚未完成。
    # 再刷新一次，重新点击暗锁并重新设置日期。
    _debug_log("[JJ] 最近一年日期验证失败；刷新后重新执行暗锁 + 日期流程")
    try:
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(800)
        retry_unlocked = await _jj_unlock_search_range(page)
        if not retry_unlocked:
            return False
        retry_date_ok = await _jj_set_one_year_date(page)
        if retry_date_ok:
            _debug_log("[JJ] 刷新后暗锁可用 + 最近一年日期均已确认")
            return True
    except Exception as e:
        _debug_log(f"[JJ] 刷新后重新准备搜索范围失败：{e!r}")
    return False


async def _jj_find_order_input(page, kind):
    if kind == "platform":
        selectors = [
            "#q_id_eq",
            "input[name='q[id_eq]']",
            "#q_id",
            "input[name='q[id]']",
            "input[placeholder*='平台订单']",
            "input[placeholder*='平台訂單']",
        ]
        label = "平台订单号"
    else:
        selectors = [
            "#q_merchant_order_id_or_order_trade_id_eq",
            "input[name='q[merchant_order_id_or_order_trade_id_eq]']",
            "#q_merchant_order_id_or_order_trade_id",
            "input[name='q[merchant_order_id_or_order_trade_id]']",
            "input[placeholder*='其他订单']",
            "input[placeholder*='其他訂單']",
        ]
        label = "其他订单号"

    loc = await _first_visible(page, selectors, timeout=5000)
    if loc:
        return loc

    # label 兜底。
    for txt in [label, label.replace("号", "號")]:
        lab = page.locator(f"label:has-text('{txt}')").first
        try:
            if await lab.count():
                target_id = await lab.get_attribute("for")
                if target_id:
                    target = page.locator(f"#{target_id}").first
                    if await target.count():
                        return target
                target = lab.locator("xpath=..//input[1]").first
                if await target.count():
                    return target
        except Exception:
            pass

    raise Exception(f"JJ 找不到【{label}】输入框")


async def _jj_search(page, order_no, kind):
    """JJ 出貨管理搜尋。

    出貨管理有兩個订单号欄位：
      1) 平台订单号 -> q[id_eq]
      2) 其他订单号 -> q[merchant_order_id_or_order_trade_id_eq]

    这里每次搜索都会先清空两个订单号字段，再只填写当前要搜索的字段，
    并直接提交 #guest_payment_order_search。搜索完成后优先检查真实结果行：
      tr#guest_payment_order_<完整UUID>
    这是当前 JJ 页面 DevTools 已确认的最可靠结果判定方式。
    """
    inp = await _jj_find_order_input(page, kind)
    form = page.locator("#guest_payment_order_search").first
    if not await form.count():
        raise Exception("JJ 出货管理找不到【guest_payment_order_search】搜索表单")

    # 清空两个订单号栏位，避免前一次搜索条件残留。
    all_order_inputs = [
        "#q_id_eq",
        "input[name='q[id_eq]']",
        "#q_id",
        "input[name='q[id]']",
        "#q_merchant_order_id_or_order_trade_id_eq",
        "input[name='q[merchant_order_id_or_order_trade_id_eq]']",
        "#q_merchant_order_id_or_order_trade_id",
        "input[name='q[merchant_order_id_or_order_trade_id]']",
    ]
    cleared = set()
    for selector in all_order_inputs:
        try:
            loc = form.locator(selector).first
            if await loc.count():
                ident = await loc.get_attribute("id") or selector
                if ident not in cleared:
                    await loc.fill("")
                    cleared.add(ident)
        except Exception:
            continue

    await inp.fill(order_no)
    _debug_log(
        f"[JJ] 出货管理准备搜索：kind={kind}, order={order_no}, "
        f"input_id={await inp.get_attribute('id')}, input_name={await inp.get_attribute('name')}"
    )

    # 记录提交前 URL，之后等待真正导航/结果刷新。
    before_url = page.url
    search_btn = await _first_visible(form, [
        "input[type='submit']",
        "button[type='submit']",
        "input[value*='搜']",
        "button:has-text('搜索')",
        "button:has-text('搜尋')",
    ], timeout=3000)

    submitted = False
    try:
        if search_btn:
            await search_btn.click()
            submitted = True
        else:
            await inp.press("Enter")
            submitted = True
    except Exception as e:
        _debug_log(f"[JJ] 点击搜索按钮失败，改用 Enter：{e!r}")
        try:
            await inp.press("Enter")
            submitted = True
        except Exception:
            pass

    if not submitted:
        raise Exception(f"JJ 出货管理【{kind}】搜索提交失败")

    # 先给正常页面导航一点时间。
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(200)

    _debug_log(f"[JJ] 出货管理搜索提交后 URL={page.url}")

    # 结果行：当前页面已确认的真实 DOM。
    exact_selector = f"xpath=//tr[@id='guest_payment_order_{order_no}']"
    try:
        await page.locator(exact_selector).wait_for(state="attached", timeout=15000)
        _debug_log(f"[JJ] 出货管理直接找到目标结果行：guest_payment_order_{order_no}")
        return
    except Exception:
        pass

    # 如果输入的是其他订单号，结果 tr 的 id 可能不是输入值；用 short-uuid 的
    # data-origin-uuid / 行文字继续等待一次。
    try:
        uuid_node = page.locator(
            f"span.short-uuid[data-origin-uuid='{order_no}']"
        ).first
        if await uuid_node.count():
            _debug_log(f"[JJ] 出货管理通过 data-origin-uuid 找到订单：{order_no}")
            return
    except Exception:
        pass

    # 最后等待页面刷新完成后再返回，让上层用 _locate_jj_result_row 做一次完整扫描。
    await page.wait_for_timeout(250)
    _debug_log(
        f"[JJ] 出货管理暂未定位到目标 tr：order={order_no}, "
        f"before_url={before_url}, after_url={page.url}"
    )



def _normalize_header(text):
    return re.sub(r"\s+", "", text or "").lower()


async def _extract_jj_row(page, order_no=""):
    """从 JJ 搜索结果中找目标订单。

    JJ 实际页面可能把结果渲染成 table、tr、role=row 或普通 div，
    而平台订单号又可能显示为 acd8c604... 的截断文字。
    因此这里不再只依赖 table/tbody tr，而是优先直接寻找订单号文字，
    再向上寻找对应的行容器；最后才使用单结果兜底。
    """
    wanted = re.sub(r"\s+", "", order_no or "").lower()
    if not wanted:
        return [], []

    prefixes = [wanted]
    if len(wanted) >= 12:
        prefixes.append(wanted[:12])
    if len(wanted) >= 8:
        prefixes.append(wanted[:8])

    def norm(v):
        return re.sub(r"\s+", "", v or "").lower()

    async def row_to_data(row):
        """把各种可能的行容器转换成 headers/cells。"""
        try:
            # 标准 table 行
            cells = row.locator(":scope > td")
            if await cells.count():
                table = row.locator("xpath=ancestor::table[1]").first
                headers = table.locator(":scope > thead > tr > th") if await table.count() else page.locator("thead > tr > th")
                header_count = await headers.count()
                header_texts = [
                    _normalize_header(await headers.nth(i).inner_text())
                    for i in range(header_count)
                ]
                cell_texts = [
                    _clean_text_value(await cells.nth(i).inner_text())
                    for i in range(await cells.count())
                ]
                if cell_texts:
                    return header_texts, cell_texts

            # role=row 或普通 div 行：优先直接拿可见文字。
            text = _clean_text_value(await row.inner_text())
            if text:
                # 如果内部存在 role=cell，则按 cell 拆分
                role_cells = row.locator("[role='cell']")
                if await role_cells.count():
                    vals = [_clean_text_value(await role_cells.nth(i).inner_text())
                            for i in range(await role_cells.count())]
                    if vals:
                        return [], vals
                # 普通 div 没有明确列时，把整行作为一格，后续用文本兜底解析
                return [], [text]
        except Exception:
            pass
        return [], []

    # 1) JJ 实际页面最可靠的识别方式。
    # 截图已确认：结果行本身的 id 就是 guest_payment_order_<完整UUID>，
    # 且 span.short-uuid 的 data-origin-uuid 也保存完整 UUID。
    # 因此先用“结果行 id”精确定位，再用 data-origin-uuid 双重确认。
    try:
        exact_row = page.locator(f"xpath=//tr[@id='guest_payment_order_{order_no}']").first
        if await exact_row.count():
            h, c = await row_to_data(exact_row)
            if c:
                return h, c
            # 即使 td 解析异常，也保留整行文字，后面的固定列解析可以继续处理。
            try:
                text = _clean_text_value(await exact_row.inner_text())
                if text:
                    return [], [text]
            except Exception:
                pass
    except Exception:
        pass

    try:
        uuid_nodes = page.locator("span.short-uuid[data-origin-uuid]")
        uuid_count = await uuid_nodes.count()
        for i in range(uuid_count):
            node = uuid_nodes.nth(i)
            try:
                # 不要求 visible；后台表格可能在滚动区域或刚刷新完成时
                # 暂时被 Playwright 判定为不可见，但 DOM 已经存在。
                origin = norm(await node.get_attribute("data-origin-uuid"))
                if origin != wanted:
                    continue

                # 截图确认标准结果行结构：
                # <tr id="guest_payment_order_<uuid>">...<span class="short-uuid" ...>
                tr = node.locator("xpath=ancestor::tr[1]").first
                if await tr.count():
                    h, c = await row_to_data(tr)
                    if c:
                        return h, c

                # 非标准表格时再向上寻找包含 td 的结果容器。
                ancestor = node.locator("xpath=ancestor::*[.//td][1]").first
                if await ancestor.count():
                    h, c = await row_to_data(ancestor)
                    if c:
                        return h, c
            except Exception:
                continue
    except Exception:
        pass

    # 2) 其次才找页面上显示的订单号/截断前缀。
    for prefix in prefixes:
        try:
            # 用 regex 忽略大小写；只要求前缀连续出现，兼容 acd8c604...。
            target = page.get_by_text(re.compile(re.escape(prefix), re.I)).first
            if await target.count() and await target.is_visible():
                # 标准 table
                tr = target.locator("xpath=ancestor::tr[1]").first
                if await tr.count():
                    h, c = await row_to_data(tr)
                    if c:
                        return h, c

                # aria/grid 行
                grid_row = target.locator("xpath=ancestor::*[@role='row'][1]").first
                if await grid_row.count():
                    h, c = await row_to_data(grid_row)
                    if c:
                        return h, c

                # 常见 bootstrap 表格/结果容器：向上找带 td 的祖先
                ancestor = target.locator("xpath=ancestor::*[.//td][1]").first
                if await ancestor.count():
                    h, c = await row_to_data(ancestor)
                    if c:
                        return h, c

                # 最后：目标元素附近的可见父容器
                parent = target.locator("xpath=..").first
                for _ in range(4):
                    if not await parent.count():
                        break
                    h, c = await row_to_data(parent)
                    if c and (len(c) > 1 or prefix in norm(" | ".join(c))):
                        return h, c
                    parent = parent.locator("xpath=..").first
        except Exception:
            continue

    # 3) 标准 table 全量扫描，兼容订单号被放在 title/data-* 属性。
    tables = page.locator("table")
    table_count = await tables.count()
    fallback = None
    for ti in range(table_count):
        table = tables.nth(ti)
        try:
            if not await table.is_visible():
                continue
            rows = table.locator("tbody > tr")
            row_count = await rows.count()
            if row_count == 0:
                rows = table.locator(":scope > tr")
                row_count = await rows.count()

            headers = table.locator("thead > tr > th")
            header_count = await headers.count()
            header_texts = [
                _normalize_header(await headers.nth(i).inner_text())
                for i in range(header_count)
            ]

            for ri in range(row_count):
                row = rows.nth(ri)
                try:
                    if not await row.is_visible():
                        continue
                except Exception:
                    pass
                row_text = _clean_text_value(await row.inner_text())
                nr = norm(row_text)
                if not row_text or any(x in row_text for x in ["没有资料", "沒有資料", "无数据", "無資料"]):
                    continue

                matched = any(x in nr for x in prefixes)
                if not matched:
                    try:
                        attrs = row.locator("[title], [data-original-title], [data-id], [data-value], a")
                        for ai in range(await attrs.count()):
                            el = attrs.nth(ai)
                            for attr in ["title", "data-original-title", "data-id", "data-value", "href"]:
                                val = await el.get_attribute(attr)
                                if val and any(x in norm(val) for x in prefixes):
                                    matched = True
                                    break
                            if matched:
                                break
                    except Exception:
                        pass

                if matched:
                    cells = row.locator(":scope > td")
                    cell_texts = [_clean_text_value(await cells.nth(i).inner_text())
                                  for i in range(await cells.count())]
                    return header_texts, cell_texts

                if row_count == 1:
                    cells = row.locator(":scope > td")
                    cell_texts = [_clean_text_value(await cells.nth(i).inner_text())
                                  for i in range(await cells.count())]
                    if cell_texts:
                        fallback = (header_texts, cell_texts)
        except Exception:
            continue

    # 4) role=row 全量扫描。
    try:
        role_rows = page.locator("[role='row']")
        rr_count = await role_rows.count()
        for i in range(rr_count):
            row = role_rows.nth(i)
            if not await row.is_visible():
                continue
            text = _clean_text_value(await row.inner_text())
            if any(x in norm(text) for x in prefixes):
                cells = row.locator("[role='cell']")
                if await cells.count():
                    return [], [_clean_text_value(await cells.nth(j).inner_text()) for j in range(await cells.count())]
                if text:
                    return [], [text]
    except Exception:
        pass

    return fallback if fallback else ([], [])


def _cell_by_header(headers, cells, keywords):
    for i, h in enumerate(headers):
        if any(k in h for k in keywords) and i < len(cells):
            return cells[i]
    return ""


async def _locate_jj_result_row(page, order_no):
    """定位本次搜索真正命中的 JJ 订单行。

    输入可能是平台订单号，也可能是商户订单号；商户订单号不会出现在
    guest_payment_order_<输入值> 的 tr id 中，因此不能只拼接 tr id。
    """
    wanted = _clean_text_value(str(order_no or ""))
    if not wanted:
        return None
    norm_wanted = re.sub(r"\s+", "", wanted).lower()

    try:
        row = page.locator(f"xpath=//tr[@id='guest_payment_order_{wanted}']").first
        if await row.count():
            return row
    except Exception:
        pass

    try:
        nodes = page.locator("span.short-uuid[data-origin-uuid]")
        for i in range(await nodes.count()):
            node = nodes.nth(i)
            origin = _clean_text_value(await node.get_attribute("data-origin-uuid"))
            if re.sub(r"\s+", "", origin).lower() == norm_wanted:
                row = node.locator("xpath=ancestor::tr[1]").first
                if await row.count():
                    return row
    except Exception:
        pass

    try:
        target = page.get_by_text(re.compile(re.escape(wanted), re.I)).first
        if await target.count():
            row = target.locator("xpath=ancestor::tr[1]").first
            if await row.count():
                return row
    except Exception:
        pass

    try:
        rows = page.locator("table tbody tr")
        for i in range(await rows.count()):
            row = rows.nth(i)
            try:
                text = _clean_text_value(await row.inner_text())
                if norm_wanted in re.sub(r"\s+", "", text).lower():
                    return row
            except Exception:
                continue
    except Exception:
        pass

    try:
        rows = page.locator("table tbody tr")
        if await rows.count() == 1:
            row = rows.first
            text = _clean_text_value(await row.inner_text())
            if text and not any(x in text for x in ["没有资料", "沒有資料", "无数据", "無資料"]):
                return row
    except Exception:
        pass
    return None


async def _extract_payment_account_from_order(page, order_no, result_row=None):
    """进入 JJ 出货订单对应的【收款帐号】详情页，读取真实收款账号。

    这里必须进入截图中的收款帐户详情页核对，不能直接从出货订单列表猜测。
    详情页实际结构为：左侧字段（例如“帳號”）+ 右侧字段值（例如邮箱/账号）。
    """
    row = result_row
    if row is None:
        row = await _locate_jj_result_row(page, order_no)
    if row is None or not await row.count():
        raise Exception(f"订单【{order_no}】找不到对应结果行，无法核对收款号。")

    # ① 从“出货平台”这一列找到收款帐户详情链接。
    link = row.locator("a[href*='/payment_settings/']").first
    if not await link.count():
        cells = row.locator(":scope > td")
        if await cells.count() > 6:
            # 你截图里的出货平台是第 7 个外层 td（索引 6）。
            link = cells.nth(6).locator("a[href]").first

    # 再做一次全行 href 扫描，避免页面版本变化导致第 7 列位置变化。
    if not await link.count():
        anchors = row.locator("a[href]")
        for i in range(await anchors.count()):
            a = anchors.nth(i)
            try:
                href0 = await a.get_attribute("href")
                if href0 and '/payment_settings/' in href0:
                    link = a
                    break
            except Exception:
                continue

    if not await link.count():
        raise Exception(f"订单【{order_no}】找不到【出货平台】收款帐户详情链接，无法核对收款号。")

    href = await link.get_attribute("href")
    if not href:
        raise Exception(f"订单【{order_no}】的收款帐户详情链接地址为空。")

    if href.startswith('/'):
        origin = re.match(r'^(https?://[^/]+)', page.url)
        href = (origin.group(1) if origin else '') + href
    elif not re.match(r'^https?://', href, re.I):
        base = re.sub(r'/[^/]*$', '/', page.url)
        href = base + href.lstrip('/')

    _debug_log(f"[出货] 进入收款帐户详情页核对账号：order={order_no}, url={href}")
    await page.goto(href, wait_until="domcontentloaded")
    await page.wait_for_timeout(250)
    _debug_log(f"[出货] 收款帐户详情页已打开：url={page.url}")

    account_label_norms = {
        '帳號', '账号', '帐号', '帳户', '账户',
        '收款号', '收款號', '收款账号', '收款帳號', '收款帐号',
        '支付帳號', '支付账号', '支付帐号',
    }
    account_keywords = ('帳號', '账号', '帐号', '帳户', '账户', '收款号', '收款號', '收款账号', '收款帳號', '收款帐号')
    payment_account = ''
    payment_method = ''

    def norm(v):
        return re.sub(r'\s+', '', _clean_text_value(v or ''))

    # ② 第一优先：严格按“字段名称 -> 下一格字段值”读取。
    try:
        rows = page.locator('table tr')
        row_count = await rows.count()
        _debug_log(f"[出货] 收款帐户详情页 table tr 数量：{row_count}")
        for i in range(row_count):
            r = rows.nth(i)
            try:
                cells = r.locator(':scope > th, :scope > td')
                count = await cells.count()
                if count < 2:
                    continue
                values = []
                for j in range(count):
                    values.append(_clean_text_value(await cells.nth(j).inner_text()))

                for j, label in enumerate(values):
                    label_norm = norm(label)
                    if label_norm in account_label_norms or any(k in label_norm for k in account_keywords):
                        # 从标签右侧寻找第一个非空值；详情页截图就是这种结构。
                        for k in range(j + 1, count):
                            candidate = _clean_text_value(values[k])
                            if candidate and norm(candidate) not in account_label_norms:
                                payment_account = candidate
                                break
                        if payment_account:
                            break
                if payment_account:
                    break

                # 兼容某些版本把“帳號 caco@163.com”放在同一个 td/th 中。
                row_text = _clean_text_value(await r.inner_text())
                row_norm = norm(row_text)
                if any(k in row_norm for k in account_keywords):
                    for k in account_keywords:
                        pos = row_norm.find(norm(k))
                        if pos >= 0:
                            raw_after = row_norm[pos + len(norm(k)):]
                            raw_after = re.sub(r'^[：:：=\-\s]+', '', raw_after)
                            if raw_after and raw_after not in account_label_norms:
                                payment_account = raw_after
                                break
                if payment_account:
                    break
            except Exception:
                continue
    except Exception as e:
        _debug_log(f"[出货] 收款帐户详情页表格读取异常：{repr(e)}")

    # ③ 第二优先：直接找“帳號”元素，再向最近的 tr 取其它单元格。
    if not payment_account:
        for label in account_keywords:
            try:
                loc = page.get_by_text(label, exact=True).first
                if not await loc.count():
                    # exact 可能因空格/换行失败，再用包含文字的定位。
                    loc = page.get_by_text(re.compile(re.escape(label), re.I)).first
                if not await loc.count():
                    continue

                tr = loc.locator('xpath=ancestor::tr[1]').first
                if await tr.count():
                    vals = tr.locator(':scope > th, :scope > td')
                    n = await vals.count()
                    texts = [_clean_text_value(await vals.nth(j).inner_text()) for j in range(n)]
                    label_idx = -1
                    for j, t in enumerate(texts):
                        if norm(t) in account_label_norms or norm(label) in norm(t):
                            label_idx = j
                            break
                    if label_idx >= 0:
                        for j in range(label_idx + 1, n):
                            if texts[j] and norm(texts[j]) not in account_label_norms:
                                payment_account = texts[j]
                                break
                    if not payment_account:
                        # 若标签和值在同一个 cell，从标签后面截取。
                        joined = _clean_text_value(await tr.inner_text())
                        joined_norm = norm(joined)
                        p = joined_norm.find(norm(label))
                        if p >= 0:
                            candidate = re.sub(r'^[：:：=\-\s]+', '', joined_norm[p + len(norm(label)):])
                            if candidate and candidate not in account_label_norms:
                                payment_account = candidate
                if payment_account:
                    break
            except Exception:
                continue

    # ④ 第三优先：从详情页“帳號”所在文本块中提取邮箱/数字账号。
    # 这一步只在“帳號”标签附近执行，不会从 QRCode 或其它字段乱抓。
    if not payment_account:
        try:
            all_rows = page.locator('tr')
            for i in range(await all_rows.count()):
                txt = _clean_text_value(await all_rows.nth(i).inner_text())
                if not txt:
                    continue
                compact = re.sub(r'\s+', '', txt)
                if not any(k in compact for k in account_keywords):
                    continue

                # 邮箱优先，例如截图中的 caco@163.com。
                m = re.search(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', txt)
                if m:
                    payment_account = m.group(0)
                    break

                # 没有邮箱时，允许常见纯数字收款账号/手机号。
                m = re.search(r'(?<!\d)\d{6,30}(?!\d)', txt)
                if m:
                    payment_account = m.group(0)
                    break
        except Exception:
            pass

    # 收款方式只作记录，不参与账号核对条件。
    try:
        for label in ['收款方式', '收款方法', '收款方式']:
            loc = page.get_by_text(label, exact=True).first
            if await loc.count():
                tr = loc.locator('xpath=ancestor::tr[1]').first
                if await tr.count():
                    vals = tr.locator(':scope > th, :scope > td')
                    if await vals.count() >= 2:
                        payment_method = _clean_text_value(await vals.nth(1).inner_text())
                if payment_method:
                    break
    except Exception:
        pass

    if not payment_account:
        # 诊断时输出详情页可见文本的前一部分，方便下一次针对真实 DOM 修正。
        try:
            visible_text = _clean_text_value(await page.locator('body').inner_text())
            _debug_log(f"[出货] 收款帐户详情页未读取到帳號；页面文字前1000字：{visible_text[:1000]}")
        except Exception:
            pass
        raise Exception(f"订单【{order_no}】的收款帐户详情页找不到【帳號/收款号】。")

    _debug_log(f"[出货] 已从收款帐户详情页读取真实收款号：order={order_no}, account={payment_account}")
    return {
        'account': payment_account,
        'method': payment_method,
        'url': page.url,
    }


def _normalize_payment_account(value):
    value = _clean_text_value(str(value or '')).strip()
    if not value:
        return ''
    # 邮箱/账号通常不区分大小写；银行号/手机号则统一只比较数字。
    if '@' in value:
        return value.lower().replace(' ', '')
    digits = re.sub(r'\D', '', value)
    if digits:
        return digits
    return re.sub(r'\s+', '', value).lower()


def _payment_account_matches(expected, actual):
    a = _normalize_payment_account(expected)
    b = _normalize_payment_account(actual)
    return bool(a and b and a == b)



async def _jj_open_pdd(page):
    """打开 JJ 的【进货管理 -> 拼多多订单管理】页面。

    JJ 左侧菜单默认会把【拼多多订单管理】收在【进货管理】下面，
    所以必须先展开【进货管理】，再点击子菜单。
    """
    _debug_log(f"[PDD] 开始打开拼多多订单管理；当前 URL={page.url}")

    # ① 先展开左侧【进货管理】。
    parent_selectors = [
        "li.treeview:has(> a span:text-is('进货管理')) > a",
        "li.treeview:has(> a span:text-is('進貨管理')) > a",
        "li.treeview:has(> a:has-text('进货管理')) > a",
        "li.treeview:has(> a:has-text('進貨管理')) > a",
    ]
    expanded = False
    parent_errors = []

    for selector in parent_selectors:
        loc = page.locator(selector).first
        try:
            count = await loc.count()
            visible = await loc.is_visible() if count else False
            _debug_log(f"[PDD] 进货管理父菜单候选 {selector}: count={count}, visible={visible}")
            if not (count and visible):
                continue

            # 已经展开时不需要重复点击；以 active / aria-expanded / 子菜单可见性判断。
            parent_li = loc.locator("..")
            cls = await parent_li.get_attribute("class") or ""
            aria = await loc.get_attribute("aria-expanded")
            child_visible = False
            for child_sel in [
                "ul.treeview-menu li a:has-text('拼多多订单管理')",
                "ul.treeview-menu li a:has-text('拼多多訂單管理')",
                "ul.treeview-menu li a:has-text('拼多多订单')",
                "ul.treeview-menu li a:has-text('拼多多訂單')",
            ]:
                child = parent_li.locator(child_sel).first
                if await child.count() and await child.is_visible():
                    child_visible = True
                    break

            if child_visible or "active" in cls or aria == "true":
                _debug_log(f"[PDD] 进货管理已经展开；class={cls!r}, aria-expanded={aria!r}")
                expanded = True
                break

            await loc.click()
            await page.wait_for_timeout(250)
            _debug_log(f"[PDD] 已点击【进货管理】展开菜单；URL={page.url}")
            expanded = True
            break
        except Exception as e:
            err = repr(e)
            parent_errors.append(f"{selector}: {err}")
            _debug_log(f"[PDD] 展开【进货管理】失败: {err}")

    if not expanded:
        detail = " | ".join(parent_errors[-3:])
        raise Exception(f"JJ 后台找不到或无法展开【进货管理】菜单；当前URL={page.url}; {detail}")

    # ② 展开后再找【拼多多订单管理】子菜单。
    child_selectors = [
        "li.treeview-menu a:has-text('拼多多订单管理')",
        "li.treeview-menu a:has-text('拼多多訂單管理')",
        "ul.treeview-menu a:has-text('拼多多订单管理')",
        "ul.treeview-menu a:has-text('拼多多訂單管理')",
        "ul.treeview-menu a:has-text('拼多多订单')",
        "ul.treeview-menu a:has-text('拼多多訂單')",
    ]
    child_errors = []

    for selector in child_selectors:
        loc = page.locator(selector).first
        try:
            count = await loc.count()
            visible = await loc.is_visible() if count else False
            _debug_log(f"[PDD] 子菜单候选 {selector}: count={count}, visible={visible}")
            if count and visible:
                href = await loc.get_attribute("href")
                _debug_log(f"[PDD] 找到【拼多多订单管理】子菜单，href={href!r}")
                await loc.click()
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(200)
                _debug_log(f"[PDD] 已点击【拼多多订单管理】；进入 URL={page.url}")
                return
        except Exception as e:
            err = repr(e)
            child_errors.append(f"{selector}: {err}")
            _debug_log(f"[PDD] 点击子菜单 {selector} 失败: {err}")

    # ③ 最后才尝试直接链接。注意：该页面实际 href 不一定包含 pdd/pinduoduo，
    # 因此不能只靠 href 关键字判断。
    fallback_links = page.locator("a").filter(
        has_text=re.compile(r"^\s*拼多多(?:订单|訂單)(?:管理)?\s*$")
    )
    try:
        count = await fallback_links.count()
        for i in range(count):
            loc = fallback_links.nth(i)
            if await loc.is_visible():
                href = await loc.get_attribute("href")
                _debug_log(f"[PDD] 文字精确匹配找到子菜单，href={href!r}")
                await loc.click()
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(200)
                _debug_log(f"[PDD] 已通过精确文字进入拼多多订单管理；URL={page.url}")
                return
    except Exception as e:
        child_errors.append(f"exact-text: {repr(e)}")
        _debug_log(f"[PDD] 精确文字点击失败: {repr(e)}")

    detail = " | ".join(child_errors[-5:])
    raise Exception(f"JJ 后台展开【进货管理】后仍找不到【拼多多订单管理】；当前URL={page.url}; {detail}")


async def _jj_search_page_order(page, order_no, kind="platform", form_ids=()):
    """在 JJ【拼多多订单管理】搜索订单号。

    重要：拼多多订单管理只有一个“订单号”输入框，和出货管理的两个
    订单号输入框不同。这里固定使用实际 DOM：
      #q_id_or_merchant_order_id_or_channel_order_id_or_order_trade_id_eq
    不再尝试出货管理的“平台订单号 / 商户订单号”两套字段，也不再二次搜索。
    """
    _debug_log(f"[PDD] 准备搜索唯一【订单号】输入框，order={order_no}, URL={page.url}")

    selectors = [
        "#q_id_or_merchant_order_id_or_channel_order_id_or_order_trade_id_eq",
        "input[name='q[id_or_merchant_order_id_or_channel_order_id_or_order_trade_id_eq]']",
    ]

    # DevTools 已确认该页面只有一个“订单号”字段；以下仅作为同一字段的
    # HTML label / class 兜底，不去寻找第二个订单号字段。
    inp = await _first_visible(page, selectors, timeout=7000)
    if not inp:
        label_texts = ["订单号", "訂單號"]
        for txt in label_texts:
            try:
                labels = page.locator("label").filter(has_text=re.compile(rf"^\\s*{re.escape(txt)}\\s*$")).first
                if await labels.count():
                    target_id = await labels.get_attribute("for")
                    if target_id:
                        target = page.locator(f"#{target_id}").first
                        if await target.count() and await target.is_visible():
                            inp = target
                            break
                    target = labels.locator("xpath=..//input[1]").first
                    if await target.count() and await target.is_visible():
                        inp = target
                        break
            except Exception:
                pass

    if not inp:
        raise Exception("JJ 拼多多订单管理找不到【订单号】输入框")

    try:
        _debug_log(
            f"[PDD] 找到唯一订单号输入框: "
            f"id={await inp.get_attribute('id')}, "
            f"name={await inp.get_attribute('name')}, "
            f"placeholder={await inp.get_attribute('placeholder')}"
        )
    except Exception:
        pass

    await inp.fill("")
    await inp.fill(order_no)

    # 实际页面表单由 DevTools 确认为 investor_reward_deposit_order_search；
    # 同时保留调用方传入的 form_ids 及通用 action 兜底。
    form = None
    form_candidates = list(form_ids) + [
        "#investor_reward_deposit_order_search",
        "form[action*='investor_reward_deposit_orders']",
        "form[action*='reward_deposit']",
    ]
    for fid in form_candidates:
        try:
            f = page.locator(fid).first
            if await f.count() and await f.is_visible():
                form = f
                break
        except Exception:
            pass

    search_btn = None
    if form is not None:
        search_btn = await _first_visible(
            form,
            [
                "input[type='submit']",
                "button[type='submit']",
                "input[value*='搜']",
                "button:has-text('搜索')",
                "button:has-text('搜尋')",
            ],
            timeout=3000,
        )

    try:
        if search_btn:
            _debug_log("[PDD] 使用拼多多唯一订单号搜索按钮提交")
            await search_btn.click()
        else:
            _debug_log("[PDD] 没找到搜索按钮，使用唯一订单号输入框 Enter 提交")
            await inp.press("Enter")
    except Exception as e:
        _debug_log(f"[PDD] 搜索按钮提交失败，改用唯一订单号输入框 Enter: {repr(e)}")
        await inp.press("Enter")

    await page.wait_for_timeout(250)
    _debug_log(f"[PDD] 唯一订单号搜索完成；当前 URL={page.url}")


async def _jj_query_pdd_order(single_order_no, task_id, session=None):
    """查询 JJ 拼多多订单管理。

    找到成功订单 -> 返回提现所需资料；找到失败订单 -> 返回 status=失败；
    完全找不到 -> 返回 None，让上层决定如何提示。
    拼多多流程不核对收款号。
    """
    if not JJ_ADMIN_URL:
        raise Exception("未检测到环境变量 JJ_ADMIN_URL！")
    if not JJ_ADMIN_USER or not JJ_ADMIN_PASS:
        raise Exception("未检测到 JJ_ADMIN_USER / JJ_ADMIN_PASS！")

    own_session = session is None
    if own_session:
        session = _ReusableBrowserSession(use_totp=True)
    page = session.page if session is not None else None
    if page is None or page.is_closed():
        page = await session.start(JJ_ADMIN_URL, JJ_ADMIN_USER, JJ_ADMIN_PASS, task_id=task_id)
    try:
        _debug_log(f"[PDD] JJ 登录完成；URL={page.url}")

        await _jj_open_pdd(page)
        _debug_log(f"[PDD] 已进入拼多多页面；URL={page.url}")
        # 拼多多订单管理同样解暗锁，并把建立日期范围拉到最近一年。
        if not await _jj_prepare_search_range(page):
            raise Exception("JJ 拼多多订单管理无法确认暗锁/最近一年日期范围已生效。")
        _debug_log("[PDD] 搜索日期暗锁与最近一年范围已验证")

        # 拼多多页面只有一个“订单号”搜索框：只搜索一次。
        # 不再套用出货管理的“平台订单号 / 商户订单号”双字段逻辑。
        await _jj_search_page_order(
            page,
            single_order_no,
            "platform",
            form_ids=("#investor_reward_deposit_order_search",),
        )
        headers, cells = await _extract_jj_row(page, single_order_no)
        result_row = await _locate_jj_result_row(page, single_order_no)

        # 有些 JJ 页面结果行已经出现，但通用解析器暂时拿不到 td；
        # 不能因此误报“订单未找到”。只要已经精确定位到目标行，就以目标行为准。
        if result_row is not None and await result_row.count() and not cells:
            try:
                direct_tds = result_row.locator(":scope > td")
                direct_count = await direct_tds.count()
                if direct_count:
                    cells = [_clean_text_value(await direct_tds.nth(i).inner_text()) for i in range(direct_count)]
                    table = result_row.locator("xpath=ancestor::table[1]").first
                    if await table.count():
                        ths = table.locator("thead > tr > th")
                        headers = [_normalize_header(await ths.nth(i).inner_text()) for i in range(await ths.count())]
            except Exception:
                pass

        _debug_log(
            f"[PDD] 唯一【订单号】查询结果: cells={len(cells)}, "
            f"result_row={bool(result_row and await result_row.count()) if result_row is not None else False}"
        )

        if not cells and (result_row is None or not await result_row.count()):
            _debug_log(f"[PDD] 订单未找到: {single_order_no}")
            return None

        # 读取目标行文字/HTML，仅针对目标订单判断状态。
        row_text = ""
        row_html = ""
        if result_row is not None and await result_row.count():
            row_text = _clean_text_value(await result_row.inner_text())
            try:
                row_html = await result_row.inner_html()
            except Exception:
                pass

        status_text = _cell_by_header(headers, cells, ["状态", "狀態"])
        direct_cells = []
        if result_row is not None and await result_row.count():
            try:
                tds = result_row.locator(":scope > td")
                direct_cells = [_clean_text_value(await tds.nth(i).inner_text()) for i in range(await tds.count())]
            except Exception:
                direct_cells = []
        if direct_cells:
            cells = direct_cells

        if len(cells) > 13:
            status_text = cells[13]
        status_source = " | ".join(x for x in [status_text, row_text, row_html] if x)

        # 目标行优先；只判断成功/失败，不把页面其它统计文字算进去。
        is_success = bool(re.search(r"成功", status_source, re.I))
        is_failed = bool(re.search(r"(?:失败|失敗)", status_source, re.I)) and not is_success
        if not is_success and not is_failed:
            # 最后只扫描当前目标行各 cell。
            for cell in cells:
                if re.search(r"成功", cell, re.I):
                    is_success = True
                    status_text = cell
                    break
                if re.search(r"(?:失败|失敗)", cell, re.I):
                    is_failed = True
                    status_text = cell
                    break

        if not is_success and not is_failed:
            _debug_log(f"[PDD] 无法判断订单状态；row_text={row_text[:1000]!r}")
            raise Exception(f"JJ 拼多多订单状态无法判断：{row_text[:1000] or '无状态资料'}")

        _debug_log(f"[PDD] 订单状态判断: success={is_success}, failed={is_failed}, status={status_text!r}")

        # 当前拼多多页面字段可能略有差异，优先 header，再按常见列位置兜底。
        order_display = _cell_by_header(headers, cells, [
            "订单号", "訂單號", "平台订单", "平台訂單", "商户订单", "商戶訂單"
        ]) or single_order_no
        amount = _cell_by_header(headers, cells, ["交易金额", "交易金額", "订单金额", "訂單金額", "金额", "金額"])
        created = _cell_by_header(headers, cells, [
            "提交时间", "提交時間", "建立时间", "建立時間", "创建时间", "創建時間"
        ])
        completed = _cell_by_header(headers, cells, [
            "成功时间", "成功時間", "完成时间", "完成時間"
        ])
        recipient_raw = _cell_by_header(headers, cells, [
            "收件人", "收件人姓名", "收件人姓名", "姓名", "实名", "實名", "商户会员", "商戶會員"
        ])

        # 兼容没有 thead 的页面：优先按截图/页面常见列读取。
        if result_row is not None and await result_row.count():
            try:
                tds = result_row.locator(":scope > td")
                n = await tds.count()
                if n:
                    direct = [_clean_text_value(await tds.nth(i).inner_text()) for i in range(n)]
                    # 常见订单管理表：提交/成功/订单号/.../金额；只在 header 没取到时使用。
                    if not created and len(direct) > 0:
                        created = direct[0]
                    if not completed and len(direct) > 1:
                        completed = direct[1]
                    if not amount:
                        for d in direct:
                            m = re.search(r"(\d+(?:\.\d+)?)\s*(?:CNY|CN¥|元)", d, re.I)
                            if m:
                                amount = m.group(1)
                                break
                    if not recipient_raw:
                        for d in direct:
                            explicit_name = _extract_explicit_real_name(d)
                            if explicit_name:
                                recipient_raw = explicit_name
                                break
            except Exception:
                pass

        # 成功时间是提现表单的完成时间。
        completed_dt = _parse_jj_datetime(completed)
        created_dt = _parse_jj_datetime(created)

        if is_failed:
            return {
                "status": "失败",
                "order_no": order_display,
                "amount": amount or "",
                "created": created or "",
                "created_dt": created_dt,
                "completed": completed or "",
                "completed_dt": completed_dt,
                "recipient": _safe_manager_name(recipient_raw),
                "payment_account": "",
                "payment_method": "",
                "payment_url": "",
                "shipment": "",
            }

        if not amount:
            raise Exception(f"JJ 拼多多成功订单【{single_order_no}】没有读取到金额。")
        if not completed_dt:
            raise Exception(f"JJ 拼多多成功订单【{single_order_no}】没有读取到成功时间。")

        return {
            "status": "成功",
            "order_no": order_display,
            "amount": amount,
            "created": created or "",
            "created_dt": created_dt,
            "completed": completed or "",
            "completed_dt": completed_dt,
            "recipient": _safe_manager_name(recipient_raw),
            "payment_account": "",
            "payment_method": "",
            "payment_url": "",
            "shipment": "",
        }
    finally:
        if own_session:
            await session.close()
def _same_form_datetime(actual, target):
    a = (actual or "").strip().replace("/", "-").replace("T", " ")
    t = (target or "").strip().replace("/", "-").replace("T", " ")
    return a[:16] == t[:16]


async def _find_input_by_label(page, labels):
    for label_text in labels:
        for lab in [
            page.locator(f"label:has-text('{label_text}')").first,
            page.locator(f"th:has-text('{label_text}')").first,
        ]:
            try:
                if await lab.count():
                    target_id = await lab.get_attribute("for")
                    if target_id:
                        target = page.locator(f"#{target_id}").first
                        if await target.count():
                            return target
                    target = lab.locator("xpath=following::input[1]").first
                    if await target.count():
                        return target
                    target = lab.locator("xpath=ancestor::tr[1]//input[1]").first
                    if await target.count():
                        return target
            except Exception:
                pass
    return None

async def _single_withdraw(account, jj_result, task_id=None, session=None):
    """单笔商城：制作商户提现管理。

    拼多多订单成功才调用；不核对收款号；银行账户保持空白。
    完成时间严格使用 JJ 拼多多订单管理的成功/完成时间。
    """
    if not SINGLE_ADMIN_URL:
        raise Exception("未检测到环境变量 SINGLE_ADMIN_URL！")
    if not SINGLE_ADMIN_USER or not SINGLE_ADMIN_PASS:
        raise Exception("未检测到 SINGLE_ADMIN_USER / SINGLE_ADMIN_PASS！")

    own_session = session is None
    if own_session:
        session = _ReusableBrowserSession(use_totp=False)
    page = session.page if session is not None else None
    if page is None or page.is_closed():
        page = await session.start(SINGLE_ADMIN_URL, SINGLE_ADMIN_USER, SINGLE_ADMIN_PASS, task_id=task_id)
    try:

        # 优先从左侧菜单进入“商户提现管理”，避免猜路径。
        menu = await _first_visible(page, [
            "a:has-text('商户提现管理')",
            "a:has-text('商戶提現管理')",
            "a:has-text('提现管理')",
            "a:has-text('提現管理')",
            "a[href*='withdraw']",
        ], timeout=5000)
        if menu:
            await menu.click()
            await page.wait_for_load_state("domcontentloaded")
        else:
            for url in [
                f"{SINGLE_ADMIN_ROOT}/withdraw_orders/new",
                f"{SINGLE_ADMIN_ROOT}/withdraws/new",
                f"{SINGLE_ADMIN_ROOT}/withdraw_orders",
                f"{SINGLE_ADMIN_ROOT}/withdraws",
            ]:
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                    if await page.locator("select[name*='merchant_id'], #withdraw_order_merchant_id, #withdraw_merchant_id").count():
                        break
                except Exception:
                    continue

        # 如果目前是列表页，寻找新增/提现按钮。
        merchant_select = await _first_visible(page, [
            "#withdraw_order_merchant_id",
            "select[name='withdraw_order[merchant_id]']",
            "#withdraw_merchant_id",
            "select[name*='withdraw'][name*='merchant_id']",
            "select[name*='merchant_id']",
        ], timeout=4000)
        if not merchant_select:
            add_link = await _first_visible(page, [
                "a:has-text('新增提现')", "a:has-text('新增提現')",
                "a:has-text('提现')", "a:has-text('提現')",
                "a[href*='/withdraw_orders/new']", "a[href*='/withdraws/new']",
            ], timeout=5000)
            if add_link:
                await add_link.click()
                await page.wait_for_load_state("domcontentloaded")
            else:
                for url in [
                    f"{SINGLE_ADMIN_ROOT}/withdraw_orders/new",
                    f"{SINGLE_ADMIN_ROOT}/withdraws/new",
                ]:
                    try:
                        await page.goto(url, wait_until="domcontentloaded")
                        if await page.locator("select[name*='merchant_id'], #withdraw_order_merchant_id, #withdraw_merchant_id").count():
                            break
                    except Exception:
                        continue

        merchant_select = await _first_visible(page, [
            "#withdraw_order_merchant_id",
            "select[name='withdraw_order[merchant_id]']",
            "#withdraw_merchant_id",
            "select[name*='withdraw'][name*='merchant_id']",
            "select[name*='merchant_id']",
        ], timeout=10000)
        if not merchant_select:
            raise Exception("商户提现页面找不到【商户】下拉框。")

        await _select_select2_by_text(page, merchant_select, account, "提现商户")
        # Select2 选择结果本身已经会触发 change；不要重复触发。
        await _wait_single_merchant_transition(page, merchant_select, account, form_kind="提现")

        # 银行账户按照你的要求：保持空白，不选择、不填写。

        # 金额：优先精确字段，再按 label 兜底。
        amount_input = await _first_visible(page, [
            "#withdraw_order_total_amount",
            "#withdraw_order_amount",
            "#withdraw_total_amount",
            "input[name='withdraw_order[total_amount]']",
            "input[name='withdraw_order[amount]']",
            "input[name*='withdraw'][name*='amount']",
            "input[name*='amount']",
        ], timeout=5000)
        if not amount_input:
            amount_input = await _find_input_by_label(page, ["金额", "金額", "提现金额", "提現金額"])
        if not amount_input:
            raise Exception("商户提现页面找不到【金额】输入框。")

        amount = re.sub(r"[^0-9.]", "", str(jj_result.get("amount", "")))
        if not amount:
            raise Exception("拼多多订单金额为空。")
        await amount_input.fill(amount)

        # 完成时间 = JJ 拼多多订单管理成功时间。
        completed_dt = jj_result.get("completed_dt")
        if not completed_dt:
            raise Exception("拼多多成功订单缺少成功时间。")
        completed_input = await _first_visible(page, [
            "#withdraw_order_completed_at",
            "#withdraw_completed_at",
            "input[name='withdraw_order[completed_at]']",
            "input[name*='completed_at']",
            "input[name*='success_at']",
        ], timeout=5000)
        if not completed_input:
            completed_input = await _find_input_by_label(page, ["完成时间", "完成時間", "成功时间", "成功時間"])
        if completed_input:
            completed_text = completed_dt.strftime("%Y-%m-%dT%H:%M")
            await completed_input.fill(completed_text)
            actual = await completed_input.input_value()
            if not _same_form_datetime(actual, completed_text):
                await completed_input.evaluate(
                    """(el, value) => {
                        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                        setter.call(el, value);
                        el.dispatchEvent(new Event('input', {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        el.dispatchEvent(new Event('blur', {bubbles:true}));
                    }""", completed_text)
                actual = await completed_input.input_value()
            if not _same_form_datetime(actual, completed_text):
                raise Exception(f"提现完成时间写入失败：目标={completed_text}，实际={actual}")

        # 订单号若提现表单有此字段则填写；没有就跳过。
        order_input = await _first_visible(page, [
            "#withdraw_order_order_no",
            "#withdraw_order_platform_order_no",
            "input[name='withdraw_order[order_no]']",
            "input[name*='order_no']",
            "input[name*='order_id']",
        ], timeout=2000)
        if order_input:
            await order_input.fill(str(jj_result.get("order_no") or ""))

        # 收件人/姓名若表单存在，按 JJ 姓名填写；数字/非人名已经转为管理员代收。
        recipient_input = await _first_visible(page, [
            "#withdraw_order_recipient_name",
            "#withdraw_order_name",
            "input[name='withdraw_order[recipient_name]']",
            "input[name='withdraw_order[name]']",
            "input[name*='recipient_name']",
        ], timeout=2000)
        if recipient_input:
            await recipient_input.fill(jj_result.get("recipient") or MANAGER_RECEIVE_NAME)

        submit = await _first_visible(page, [
            "input[type='submit'][name='commit'][value='送出']",
            "input[type='submit'][value='送出']",
            "input[name='commit']",
            "button[type='submit']",
        ], timeout=8000)
        if not submit:
            raise Exception("商户提现页面找不到【送出】按钮。")

        await submit.click()
        await page.wait_for_load_state("domcontentloaded")
        return "已送出"
    finally:
        if own_session:
            await session.close()
async def _query_jj_order(single_order_no, task_id, session=None):
    if not JJ_ADMIN_URL:
        raise Exception("未检测到环境变量 JJ_ADMIN_URL！")
    if not JJ_ADMIN_USER or not JJ_ADMIN_PASS:
        raise Exception("未检测到 JJ_ADMIN_USER / JJ_ADMIN_PASS！")
    if not single_order_no:
        raise Exception("单笔订单号为空！")

    own_session = session is None
    if own_session:
        session = _ReusableBrowserSession(use_totp=True)
    page = session.page if session is not None else None
    if page is None or page.is_closed():
        page = await session.start(JJ_ADMIN_URL, JJ_ADMIN_USER, JJ_ADMIN_PASS, task_id=task_id)
    try:

        # JJ 出货管理页面。JJ_ADMIN_URL 可能是站点根地址，也可能已经包含 /admin。
        jj_base = JJ_ADMIN_URL.rstrip('/')
        if re.search(r'/admin$', jj_base, re.I):
            jj_outbound_url = jj_base + "/guest_payment_orders"
        elif re.search(r'/sign_in$', jj_base, re.I):
            jj_outbound_url = re.sub(r'/sign_in$', '', jj_base, flags=re.I) + "/guest_payment_orders"
        else:
            jj_outbound_url = jj_base + "/admin/guest_payment_orders"
        await page.goto(jj_outbound_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(400)

        # 必须先确认暗锁真的解除，再把建立日期范围拉回一年。
        if not await _jj_prepare_search_range(page):
            raise Exception("JJ 出货管理无法确认暗锁已解除 / 最近一年日期范围已生效。")

        # 先查平台订单号。
        await _jj_search(page, single_order_no, "platform")

        # 【关键修正】当前 JJ 页面已确认真实结果行就是：
        # <tr id="guest_payment_order_<完整UUID>">
        # 因此先直接定位这个 tr，再从这个 tr 读取所有字段。
        result_row = await _locate_jj_result_row(page, single_order_no)
        headers, cells = ([], [])
        if result_row is not None and await result_row.count():
            try:
                cells_loc = result_row.locator(":scope > td")
                cell_count = await cells_loc.count()
                cells = [_clean_text_value(await cells_loc.nth(i).inner_text())
                         for i in range(cell_count)]
                table = result_row.locator("xpath=ancestor::table[1]").first
                if await table.count():
                    ths = table.locator("thead > tr > th")
                    headers = [_normalize_header(await ths.nth(i).inner_text())
                               for i in range(await ths.count())]
                _debug_log(f"[出货] 平台订单号直接命中结果行：cells={len(cells)}, result_row=True")
            except Exception as e:
                _debug_log(f"[出货] 目标结果行读取失败：{e!r}")

        # 如果平台订单号确实没有命中结果行，再查第二个“其他订单号”栏位。
        if not result_row or not await result_row.count():
            _debug_log(f"[出货] 平台订单号未命中结果行，改查【其他订单号】：order={single_order_no}")
            await _jj_search(page, single_order_no, "other")
            result_row = await _locate_jj_result_row(page, single_order_no)
            if result_row is not None and await result_row.count():
                try:
                    cells_loc = result_row.locator(":scope > td")
                    cell_count = await cells_loc.count()
                    cells = [_clean_text_value(await cells_loc.nth(i).inner_text())
                             for i in range(cell_count)]
                    table = result_row.locator("xpath=ancestor::table[1]").first
                    if await table.count():
                        ths = table.locator("thead > tr > th")
                        headers = [_normalize_header(await ths.nth(i).inner_text())
                                   for i in range(await ths.count())]
                    _debug_log(f"[出货] 其他订单号命中结果行：cells={len(cells)}, result_row=True")
                except Exception as e:
                    _debug_log(f"[出货] 其他订单号目标结果行读取失败：{e!r}")

        # 搜索后给 JJ 前端 AJAX/表格渲染一个短暂轮询窗口，避免偶发“页面已经搜到，
        # 但 Playwright 恰好在结果尚未挂载时就判定不存在”。
        if result_row is None or not await result_row.count():
            for _ in range(20):
                await page.wait_for_timeout(250)
                result_row = await _locate_jj_result_row(page, single_order_no)
                if result_row is not None and await result_row.count():
                    _debug_log(f"[出货] 延迟轮询后找到目标订单：{single_order_no}")
                    break

        # 只有真正找不到目标结果行，才认定 JJ 出货订单不存在。
        if result_row is None or not await result_row.count():
            raise JJOrderNotFound(f"JJ 找不到订单：{single_order_no}")

        # 极少数页面版本无法直接读取 td 时，再使用旧的通用解析器作为兜底。
        if not cells:
            headers, cells = await _extract_jj_row(page, single_order_no)

        # ===== JJ 状态判断：必须只从“目标订单那一行”读取 =====
        # 你提供的 DevTools 已确认：目标结果是
        # <tr id="guest_payment_order_<完整UUID>"> ... </tr>
        # 页面右侧状态栏显示“成功（已補單）”等文字。
        # 这里不再依赖 thead，也不再依赖列顺序。
        status_text = _cell_by_header(headers, cells, ["状态", "狀態"])
        full_row = " | ".join(cells)

        exact_status_row = result_row
        row_status_text = ""
        row_html = ""
        try:
            if exact_status_row is not None and await exact_status_row.count():
                row_status_text = _clean_text_value(await exact_status_row.inner_text())
                try:
                    row_html = await exact_status_row.inner_html()
                except Exception:
                    row_html = ""
        except Exception:
            exact_status_row = None

        # 有些 JJ 版本会把状态放在 badge/span 的 title、data-* 或 class 中，
        # 因此同时扫描目标订单行的文字 + HTML 属性。
        status_sources = [row_status_text, status_text, full_row, row_html]
        if exact_status_row is not None:
            try:
                status_nodes = exact_status_row.locator(
                    "[title], [data-original-title], [data-status], "
                    "[data-value], .label, .badge, .status, [class*='status'], "
                    "[class*='success'], [class*='danger'], [class*='failed']"
                )
                for si in range(await status_nodes.count()):
                    node = status_nodes.nth(si)
                    try:
                        txt = _clean_text_value(await node.inner_text())
                        if txt:
                            status_sources.append(txt)
                    except Exception:
                        pass
                    for attr in ["title", "data-original-title", "data-status", "data-value", "class"]:
                        try:
                            val = await node.get_attribute(attr)
                            if val:
                                status_sources.append(val)
                        except Exception:
                            pass
            except Exception:
                pass

        combined_status_source = " | ".join(x for x in status_sources if x)

        # 只接受明确的成功/失败关键词。
        # 成功订单：有“成功”即可，不要求一定出现“已補單”。
        # 失败订单：允许繁简体。
        success_match = re.search(r"成功", combined_status_source, re.I)
        failed_match = re.search(r"(?:失败|失敗)", combined_status_source, re.I)

        # 如果页面同时出现“成功订单数”等统计文字，绝不能拿它判断。
        # 此处优先使用目标 tr；只有目标 tr 完全无法定位时才使用 cells。
        if exact_status_row is not None and await exact_status_row.count():
            target_sources = [row_status_text, row_html]
            if exact_status_row is not None:
                try:
                    target_nodes = exact_status_row.locator(
                        ".label, .badge, .status, [class*='status'], "
                        "[class*='success'], [class*='danger'], [class*='failed'], "
                        "[title], [data-original-title], [data-status]"
                    )
                    for ti in range(await target_nodes.count()):
                        node = target_nodes.nth(ti)
                        try:
                            target_sources.append(_clean_text_value(await node.inner_text()))
                        except Exception:
                            pass
                        for attr in ["title", "data-original-title", "data-status", "class"]:
                            try:
                                val = await node.get_attribute(attr)
                                if val:
                                    target_sources.append(val)
                            except Exception:
                                pass
                except Exception:
                    pass
            target_status_source = " | ".join(x for x in target_sources if x)
            target_success = bool(re.search(r"成功", target_status_source, re.I))
            target_failed = bool(re.search(r"(?:失败|失敗)", target_status_source, re.I))
            if target_success or target_failed:
                is_success = target_success
                is_failed = target_failed and not target_success
            else:
                is_success = False
                is_failed = False
        else:
            is_success = bool(success_match)
            is_failed = bool(failed_match) and not is_success

        # 最后的 cells 兜底：优先检查 JJ 当前页面确认的第 14 个外层栏位（索引 13）。
        if not is_success and not is_failed and len(cells) > 13:
            direct_status = _clean_text_value(cells[13])
            if re.search(r"成功", direct_status, re.I):
                is_success = True
                status_text = direct_status
            elif re.search(r"(?:失败|失敗)", direct_status, re.I):
                is_failed = True
                status_text = direct_status

        # 最后的 cells 兜底：只扫描当前目标订单行，不扫描页面其它区域。
        if not is_success and not is_failed:
            for cell in cells:
                nc = _clean_text_value(cell)
                if re.search(r"成功", nc, re.I):
                    is_success = True
                    status_text = nc
                    break
                if re.search(r"(?:失败|失敗)", nc, re.I):
                    is_failed = True
                    status_text = nc
                    break

        if not is_success and not is_failed:
            raise Exception(f"JJ 订单状态无法判断：{row_status_text[:1000] or full_row[:1000]}")

        # JJ 当前实际 DOM 已由 DevTools 确认：
        # 目标订单 tr#guest_payment_order_<UUID> 的外层 td 顺序固定为：
        # 0 提交时间、1 完成时间、2 订单号、3 平台会员、4 採購方、
        # 5 商户会员、6 出货平台、7 交易金额、8 金流、9 图片、10 等待时长、
        # 11 到期时间、12 异常回报、13 状态、14 操作。
        #
        # 【重要】这里不再依赖 thead/header 来读取“提交时间”。
        # 直接从目标订单 tr 的第 0 个 td 读取，避免页面表头变化导致
        # “找到订单后却读取不到提交时间”。
        order_no = _cell_by_header(headers, cells, ["平台订单", "平台訂單", "订单号", "訂單號"])
        recipient_raw = _cell_by_header(headers, cells, ["实名", "實名"])
        merchant_member_raw = _cell_by_header(headers, cells, ["商户会员", "商戶會員"])
        amount = _cell_by_header(headers, cells, ["交易金额", "交易金額", "金额", "金額"])
        created = _cell_by_header(headers, cells, [
            "建立时间", "建立時間", "创建时间", "創建時間",
            "提交时间", "提交時間"
        ])
        completed = _cell_by_header(headers, cells, ["完成时间", "完成時間"])

        # 直接从目标 tr 读取固定列；这是当前 JJ 页面最可靠的来源。
        try:
            exact_data_row = result_row
            if exact_data_row is not None and await exact_data_row.count():
                direct_cells = exact_data_row.locator(":scope > td")
                direct_count = await direct_cells.count()
                if direct_count >= 8:
                    direct_texts = [
                        _clean_text_value(await direct_cells.nth(i).inner_text())
                        for i in range(direct_count)
                    ]
                    if direct_texts:
                        # 提交时间永远优先使用第 0 格；不要使用完成时间替代。
                        created = direct_texts[0]
                        if direct_count > 1:
                            completed = direct_texts[1]
                        if direct_count > 2 and not order_no:
                            order_no = direct_texts[2]
                        if direct_count > 5 and not recipient_raw:
                            # 第 5 格是“商户会员”，只有其中明确出现“实名：xxx”时才读取；
                            # 没有显示实名就必须使用“管理员代收”，不能猜其它姓名。
                            merchant_member_raw = direct_texts[5]
                            recipient_raw = _extract_explicit_real_name(merchant_member_raw)
                        if direct_count > 7 and not amount:
                            amount = direct_texts[7]
                        if direct_count > 13:
                            status_text = direct_texts[13]
                        # 保留目标行的完整外层单元格供后续貨運解析。
                        cells = direct_texts
        except Exception:
            pass

        # 没有标准 tr 时才使用前面的 cells 位置兜底。
        if len(cells) >= 8:
            if not recipient_raw and len(cells) > 5:
                recipient_raw = _extract_explicit_real_name(cells[5])
            if not amount and len(cells) > 7:
                amount = cells[7]
            if not created and len(cells) > 0:
                created = cells[0]
            if not completed and len(cells) > 1:
                completed = cells[1]
            if not status_text and len(cells) > 13:
                status_text = cells[13]

        # JJ 后台目前显示为「貨運」；同时兼容未来改成简体「货运」、
        # 「运单号/運單號」等字段名称。失败订单没有貨運是正常状态。
        shipment = _cell_by_header(headers, cells, [
            "运单号", "運單號", "货运", "貨運",
            "物流单号", "物流單號", "货号", "貨號"
        ])
        # 如果表头定位不到（某些 JJ 版本没有标准 thead），
        # 当前页面的第一列就是提交时间，直接使用第一格。
        if not created and cells:
            first_cell = _clean_text_value(cells[0])
            if re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[年./-]\d{1,2}[月./-]\d{1,2}", first_cell):
                created = first_cell

        # 没有 header 时，从整行文本中提取金额/日期。
        if not amount:
            for cell in cells:
                m = re.search(r"(\d+(?:\.\d+)?)\s*(?:CNY|CN¥|元)", cell, re.I)
                if m:
                    amount = m.group(1)
                    break

        # 【重要】建立时间只认 JJ 的“提交时间”（第 0 格）。
        # 不再用“完成时间”兜底，避免真正的提交时间读取失败时被悄悄替换。
        created_dt = _parse_jj_datetime(created)
        if not created_dt:
            raw_time = created or ""
            try:
                created_dt = datetime.fromisoformat(
                    raw_time.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except Exception:
                pass

        if is_success and not shipment:
            # 只有成功订单才尝试从整行找运单号。
            for cell in cells:
                m = re.search(r"(?:运单号|運單號|物流单号|物流單號|货运|貨運)\s*[:：]?\s*([A-Za-z0-9_-]+)", cell, re.I)
                if m:
                    shipment = m.group(1)
                    break

        # 出货管理只要找到订单，不论成功或失败，都要进入“收款帐户”详情页核对帳號。
        # 失败订单没有運單號/配送时间是正常状态；这些字段保持空白，但仍然要继续制作本笔充值。
        payment_detail = await _extract_payment_account_from_order(
            page, single_order_no, result_row=result_row
        )
        payment_account = payment_detail['account']

        if not created_dt:
            raise Exception(f"JJ 出货订单无法读取【提交时间】：{created[:200]}")
        if not amount:
            raise Exception("JJ 出货订单没有读取到交易金额。")

        # 只有成功订单才要求完成时间；失败订单没有配送/完成时间是正常的。
        completed_dt = _parse_jj_datetime(completed) if is_success else None
        if is_success and not completed_dt:
            raise Exception(f"JJ 成功订单无法读取【完成时间/成功时间】：{completed[:200]}")

        return {
            "status": "成功" if is_success else "失败",
            "order_no": order_no or single_order_no,
            "recipient": _safe_real_name_from_order(recipient_raw),
            "amount": amount,
            # 失败订单没有運單號/配送时间；保持空白。
            "shipment": shipment if is_success else "",
            "created": created,
            "created_dt": created_dt,
            "completed": completed if is_success else "",
            "completed_dt": completed_dt if is_success else None,
            "delivery": completed_dt if is_success else None,
            "payment_account": payment_account,
            "payment_method": payment_detail.get("method", ""),
            "payment_url": payment_detail.get("url", ""),
            "raw_headers": headers,
            "raw_cells": cells,
        }
    finally:
        if own_session:
            await session.close()
async def _select_select2_by_text(page, native_select, target_text, label_name="下拉框"):
    """稳健选择 Select2 商户：支持原生 option、AJAX、搜索延迟，并最终验证真的选中。"""
    target = _clean_text_value(str(target_text or ""))
    if not target:
        raise Exception(f"{label_name}目标值为空。")

    def norm(v):
        return re.sub(r"\s+", "", (v or "")).lower()

    async def verify_selected():
        try:
            value = await native_select.input_value()
        except Exception:
            value = ""
        if not value:
            return False
        try:
            selected = native_select.locator("option:checked").first
            if await selected.count():
                text = _clean_text_value(await selected.inner_text())
                if target.lower() in text.lower() or norm(target) in norm(text):
                    return True
        except Exception:
            pass
        # 有些 Select2 option 没有稳定文字，只要 value 已产生也视为候选成功；
        # 但必须确保不是空值。
        return bool(value)

    async def scan_options():
        try:
            options = native_select.locator("option")
            for i in range(await options.count()):
                opt = options.nth(i)
                value = await opt.get_attribute("value")
                text = _clean_text_value(await opt.inner_text())
                hay = f"{text} {value or ''}"
                if value and (target.lower() == text.lower() or target.lower() in hay.lower() or norm(target) == norm(text)):
                    try:
                        await native_select.select_option(value=value)
                        await native_select.evaluate("el => el.dispatchEvent(new Event('change', {bubbles:true}))")
                        await page.wait_for_timeout(300)
                        if await verify_selected():
                            return True
                    except Exception:
                        continue
        except Exception:
            pass
        return False

    # 第一次先检查当前已经加载的 option。
    if await scan_options():
        return True

    # 找 Select2 容器。
    container = None
    candidates = [
        native_select.locator("xpath=following-sibling::span[contains(@class,'select2-container')]").first,
        native_select.locator("xpath=following-sibling::*[contains(@class,'select2-container')]").first,
        native_select.locator("xpath=..//span[contains(@class,'select2-container')]").first,
        native_select.locator("xpath=..//*[contains(@class,'select2-container')]").first,
    ]
    for c in candidates:
        try:
            if await c.count():
                container = c
                break
        except Exception:
            pass

    sid = None
    try:
        sid = await native_select.get_attribute("id")
    except Exception:
        pass

    # 最多重试 3 次：后台 AJAX 商户列表有明显延迟时，第一次打开可能还没有结果。
    last_detail = ""
    for attempt in range(3):
        try:
            if container is not None:
                await container.click(force=True)
            elif sid:
                c = page.locator(f"span.select2-container[aria-labelledby='select2-{sid}-container']").first
                if await c.count():
                    await c.click(force=True)
                else:
                    # Select2 标准容器的最后兜底。
                    c = page.locator(".select2-container").last
                    if await c.count():
                        await c.click(force=True)
            else:
                c = page.locator(".select2-container").last
                if await c.count():
                    await c.click(force=True)
        except Exception as e:
            last_detail = repr(e)

        # 等待 Select2 搜索框出现；商户 AJAX 初始化本身可能需要一点时间。
        search = page.locator(".select2-container--open input.select2-search__field, .select2-container--open input.select2-search_field, .select2-container--open input[type='search']").last
        try:
            await search.wait_for(state="visible", timeout=7000)
        except Exception:
            try:
                search = page.locator("input.select2-search__field, input.select2-search_field").last
                await search.wait_for(state="visible", timeout=3000)
            except Exception as e:
                last_detail = repr(e)
                continue

        try:
            # type 比 fill 更接近真人输入，能触发旧版 Select2 的 keyup/change 监听。
            await search.fill("")
            await search.type(target, delay=35)
        except Exception as e:
            last_detail = repr(e)
            continue

        # AJAX 搜索结果轮询最长约 6 秒；不再只等 400ms。
        for _ in range(30):
            await page.wait_for_timeout(200)
            if await scan_options():
                return True

            result_selectors = [
                ".select2-container--open .select2-results__option",
                ".select2-container--open li[role='option']",
                ".select2-results__option",
            ]
            found_any = False
            for sel in result_selectors:
                results = page.locator(sel)
                try:
                    count = await results.count()
                except Exception:
                    continue
                for i in range(count):
                    item = results.nth(i)
                    try:
                        if not await item.is_visible():
                            continue
                        txt = _clean_text_value(await item.inner_text())
                        if not txt or "正在搜尋" in txt or "Searching" in txt:
                            continue
                        found_any = True
                        if target.lower() in txt.lower() or txt.lower() in target.lower() or norm(target) in norm(txt):
                            await item.click(force=True)
                            # 单笔商城选择商户后会有明显 AJAX / 页面切换延迟。
                            # 不要 500ms 后就判定失败，最多轮询 10 秒，确认原生 select 真正产生 value。
                            for _merchant_wait in range(50):
                                await page.wait_for_timeout(200)
                                if await verify_selected():
                                    return True
                                # 某些 Select2 模板原生 option 更新更慢，但显示文字已经完成。
                                try:
                                    rendered = page.locator(
                                        ".select2-container--default .select2-selection__rendered, "
                                        ".select2-selection__rendered"
                                    ).last
                                    if await rendered.count() and await rendered.is_visible():
                                        rtxt = _clean_text_value(await rendered.inner_text())
                                        if target.lower() in rtxt.lower() or norm(target) in norm(rtxt):
                                            await page.wait_for_timeout(300)
                                            if await verify_selected():
                                                return True
                                except Exception:
                                    pass
                    except Exception:
                        continue
            if found_any:
                last_detail = "已有搜索结果，但没有匹配到目标商户"

        # 这一轮没有成功，关闭下拉后重新打开，给 Select2/AJAX 一个干净状态。
        try:
            await search.press("Escape")
        except Exception:
            pass
        await page.wait_for_timeout(300)

    # 最后一次检查：AJAX 结果可能已经插入 option，但页面事件晚了一点。
    if await scan_options():
        return True

    raise Exception(f"{label_name}找不到【{target}】。{last_detail}" if last_detail else f"{label_name}找不到【{target}】。")


def _payment_account_from_info(info):
    info_type = info.get("type", "alipay")
    if info_type == "bank":
        return info.get("bank_account", "")
    if info_type == "digital_wallet":
        return info.get("digital_account", "")
    return info.get("alipay_account", "")



async def _wait_single_merchant_transition(page, merchant_select, account, form_kind="充值"):
    """等待单笔商城选择商户后的异步处理/跳转完成。

    单笔商城的充值、提现页面在 Select2 选中商户后，后台不会立刻准备好后续表单，
    而是会经过 AJAX/JS 处理并短暂卡顿后才完成页面状态切换。
    这里不固定 sleep 很久，而是：
      1) 至少给前端 1 秒完成第一轮事件；
      2) 轮询最多 10 秒，确认商户仍为目标值且后续表单已经可用；
      3) 如果页面发生导航，等待导航后的 DOM 稳定；
      4) 超时才报错，避免把“正在加载”误判成“商户不存在”。
    """
    target = _clean_text_value(str(account or ""))
    if not target:
        raise Exception(f"{form_kind}商户目标为空。")

    await page.wait_for_timeout(1000)

    # 两类页面都至少应该存在这些后续字段中的一部分。
    if form_kind == "充值":
        readiness_selectors = [
            "#deposit_order_shipment_info_id",
            "#deposit_order_total_amount",
            "#deposit_order_created_at",
            "#deposit_order_completed_at",
        ]
    else:
        readiness_selectors = [
            "#withdraw_order_total_amount",
            "#withdraw_order_amount",
            "#withdraw_total_amount",
            "input[name*='withdraw'][name*='amount']",
        ]

    async def selected_matches():
        try:
            value = await merchant_select.input_value()
        except Exception:
            value = ""
        if not value:
            return False
        try:
            checked = merchant_select.locator("option:checked").first
            if await checked.count():
                text = _clean_text_value(await checked.inner_text())
                if target.lower() in text.lower() or re.sub(r"\s+", "", target).lower() in re.sub(r"\s+", "", text).lower():
                    return True
        except Exception:
            pass
        return True

    last_url = page.url
    for _ in range(45):  # 9 秒轮询
        await page.wait_for_timeout(200)

        # 导航完成后，重新等待 DOM。
        if page.url != last_url:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            last_url = page.url

        if not await selected_matches():
            continue

        # 商户选中 + 后续表单出现 = 异步切换完成。
        for sel in readiness_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    return
            except Exception:
                continue

        # 有些页面后续字段不是 visible，但已经存在；此时也说明 DOM 已稳定。
        for sel in readiness_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    return
            except Exception:
                continue

    raise Exception(
        f"{form_kind}选择商户【{target}】后后台异步处理/跳转超过 10 秒，未进入可填写状态。"
        f" 当前URL：{page.url}"
    )

async def _single_recharge(account, jj_result, payment_info=None, task_id=None, session=None):
    """重新登录单笔商城并填写充值；不依赖建店时已关闭的浏览器页面。"""
    if not SINGLE_ADMIN_URL:
        raise Exception("未检测到环境变量 SINGLE_ADMIN_URL！")
    if not SINGLE_ADMIN_USER or not SINGLE_ADMIN_PASS:
        raise Exception("未检测到 SINGLE_ADMIN_USER / SINGLE_ADMIN_PASS！")

    own_session = session is None
    if own_session:
        session = _ReusableBrowserSession(use_totp=False)
    page = session.page if session is not None else None
    if page is None or page.is_closed():
        page = await session.start(SINGLE_ADMIN_URL, SINGLE_ADMIN_USER, SINGLE_ADMIN_PASS, task_id=task_id)
    try:

        # 充值页面截图对应左侧「商户充值管理」。优先从菜单进入，避免猜 URL。
        menu = await _first_visible(page, [
            "a:has-text('商户充值管理')",
            "a:has-text('商戶充值管理')",
        ], timeout=5000)
        if menu:
            await menu.click()
            await page.wait_for_load_state("domcontentloaded")
        else:
            # URL 兜底。
            for url in [
                f"{SINGLE_ADMIN_ROOT}/deposit_orders",
                f"{SINGLE_ADMIN_ROOT}/deposits",
            ]:
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                    if await page.locator("#deposit_order_merchant_id").count():
                        break
                except Exception:
                    continue

        # 如果菜单打开的是列表页，再找「充值/新增」入口。原生 select2 下拉可能是隐藏的，不能用 is_visible 判断。
        merchant_select = page.locator("#deposit_order_merchant_id").first
        if await merchant_select.count() == 0:
            merchant_select = page.locator("select[name='deposit_order[merchant_id]']").first
        if await merchant_select.count() == 0:
            merchant_select = None
        if not merchant_select:
            add_link = await _first_visible(page, [
                "a:has-text('新增充值')",
                "a:has-text('充值')",
                "a[href*='/deposit_orders/new']",
                "a[href*='/deposits/new']",
            ], timeout=5000)
            if add_link:
                await add_link.click()
                await page.wait_for_load_state("domcontentloaded")
            else:
                # 最后直接尝试已知 Rails 新增地址。
                for url in [
                    f"{SINGLE_ADMIN_ROOT}/deposit_orders/new",
                    f"{SINGLE_ADMIN_ROOT}/deposits/new",
                ]:
                    try:
                        await page.goto(url, wait_until="domcontentloaded")
                        if await page.locator("#deposit_order_merchant_id").count():
                            break
                    except Exception:
                        continue

        merchant_select = page.locator("#deposit_order_merchant_id").first
        if await merchant_select.count() == 0:
            merchant_select = page.locator("select[name='deposit_order[merchant_id]']").first
        if await merchant_select.count() == 0:
            merchant_select = page.locator("select[name*='merchant_id']").first
        if await merchant_select.count() == 0:
            raise Exception("单笔商城充值页面找不到【商户】下拉框。")

        # 商户是 Select2 动态下拉。优先直接匹配 option；如果后台用 AJAX，
        # 就打开下拉，输入刚建立的商户帐号，等待并点击结果。
        try:
            await _select_select2_by_text(page, merchant_select, account, "充值商户")
        except Exception as e:
            raise Exception(f"充值商户选择失败：{e}")

        # Select2 选择结果本身已经会触发 change；不要再次手动触发，
        # 避免后台的异步商户切换逻辑被执行两次。
        await _wait_single_merchant_transition(page, merchant_select, account, form_kind="充值")

        # 【重要】充值页面的【銀行帳戶】按照用户最新确认：保持空白，不填写。
        # Telegram 中的支付宝/数字/银行卡资料仍可用于建店流程；
        # 但这里的“新增充值”表单不要选择或填写銀行帳戶。

        # 收件人资讯 = 任意一个现有选项。截图确认 ID 为 shipment_info_id。
        recipient_info = page.locator("#deposit_order_shipment_info_id").first
        if await recipient_info.count():
            opts = recipient_info.locator("option")
            selected = False
            for i in range(await opts.count()):
                opt = opts.nth(i)
                value = await opt.get_attribute("value")
                disabled = await opt.is_disabled()
                text = _clean_text_value(await opt.inner_text())
                if value and not disabled and text not in ("请选择", "請選擇"):
                    await recipient_info.select_option(value=value)
                    selected = True
                    break
            if not selected:
                raise Exception("充值页面没有可用的【收件人资讯】选项。")
            try:
                await recipient_info.evaluate("el => el.dispatchEvent(new Event('change', {bubbles:true}))")
            except Exception:
                pass
        else:
            raise Exception("充值页面找不到【收件人资讯】下拉框。")

        # 买家留言留空。
        comment = page.locator("#deposit_order_buyer_comment").first
        if await comment.count():
            await comment.fill("")

        status = jj_result.get("status")
        if status not in ("成功", "失败"):
            raise Exception("JJ 订单状态无效。")

        # 运单号：只有成功订单填写。
        if status == "成功":
            shipment = jj_result.get("shipment", "")
            if shipment:
                await page.locator("#deposit_order_shipment_no").first.fill(shipment)

        # 收件人姓名。
        recipient = jj_result.get("recipient") or MANAGER_RECEIVE_NAME
        await page.locator("#deposit_order_recipient_name").first.fill(recipient)

        # 金额。
        amount = re.sub(r"[^0-9.]", "", str(jj_result.get("amount", "")))
        if not amount:
            raise Exception("JJ 订单金额为空。")
        await page.locator("#deposit_order_total_amount").first.fill(amount)

        # 商城“配送时间/完成时间”：成功订单使用 JJ【提交时间】之后 1~2 天，
        # 白天 08:00~18:00 随机；不能直接使用 JJ 的完成时间。
        # 失败订单没有配送时间，保持空白。
        if status == "成功":
            created_dt = jj_result.get("created_dt")
            if not created_dt:
                raise Exception("成功订单缺少 JJ 后台提交时间，无法计算配送时间。")

            delivery_dt = _random_delivery_time(created_dt)
            completed_input = page.locator("#deposit_order_completed_at").first
            if await completed_input.count():
                completed_text = delivery_dt.strftime("%Y-%m-%dT%H:%M")
                await completed_input.fill(completed_text)
                actual_completed = await completed_input.input_value()

                def _same_form_datetime(actual, target):
                    a = (actual or "").strip().replace("/", "-").replace("T", " ")
                    t = (target or "").strip().replace("/", "-").replace("T", " ")
                    return a[:16] == t[:16]

                if not _same_form_datetime(actual_completed, completed_text):
                    await completed_input.evaluate(
                        """(el, value) => {
                            const setter = Object.getOwnPropertyDescriptor(
                                HTMLInputElement.prototype, 'value'
                            ).set;
                            setter.call(el, value);
                            el.dispatchEvent(new Event('input', {bubbles:true}));
                            el.dispatchEvent(new Event('change', {bubbles:true}));
                            el.dispatchEvent(new Event('blur', {bubbles:true}));
                        }""",
                        completed_text,
                    )
                    actual_completed = await completed_input.input_value()
                if not _same_form_datetime(actual_completed, completed_text):
                    raise Exception(
                        f"配送时间写入失败：目标={completed_text}，实际={actual_completed}"
                    )

        # 建立时间 = JJ【提交时间】。充值页面通常是 datetime-local，
        # 不能把 JJ 显示的“01月19日 08:30”原文直接 fill，否则浏览器会拒绝，
        # 然后留下表单默认的当前时间。这里必须使用已经解析出的 created_dt。
        created_dt = jj_result.get("created_dt")
        if not created_dt:
            raise Exception("JJ 订单缺少可用的提交时间。")

        created_input = page.locator("#deposit_order_created_at").first
        if await created_input.count():
            created_text = created_dt.strftime("%Y-%m-%dT%H:%M")
            await created_input.fill(created_text)
            actual_created = await created_input.input_value()
            # 同样兼容后台返回 YYYY/MM/DD HH:MM 的显示格式。
            def _same_created_datetime(actual, target):
                a = (actual or "").strip().replace("/", "-").replace("T", " ")
                t = (target or "").strip().replace("/", "-").replace("T", " ")
                return a[:16] == t[:16]

            if not _same_created_datetime(actual_created, created_text):
                await created_input.evaluate(
                    """(el, value) => {
                        const setter = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, 'value'
                        ).set;
                        setter.call(el, value);
                        el.dispatchEvent(new Event('input', {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        el.dispatchEvent(new Event('blur', {bubbles:true}));
                    }""",
                    created_text,
                )
                actual_created = await created_input.input_value()
            if not _same_created_datetime(actual_created, created_text):
                raise Exception(
                    f"建立时间写入失败：目标={created_text}，实际={actual_created}"
                )

        submit = await _first_visible(page, [
            "input[type='submit'][name='commit'][value='送出']",
            "input[type='submit'][value='送出']",
            "input[name='commit']",
            "button[type='submit']",
        ], timeout=8000)
        if not submit:
            raise Exception("充值页面找不到【送出】按钮。")

        await submit.click()
        await page.wait_for_load_state("domcontentloaded")
        return "已送出"
    finally:
        if own_session:
            await session.close()
async def update_shop_skin(account_name: str, new_skin: str, backend: str = "all"):
    """
    修改指定商城的界面。
    backend=all    -> 全部商城后台
    backend=single -> 单笔商城后台

    重要：不能把单笔商城的账号拿去 BASE_ADMIN_URL 搜索。
    单笔商城必须使用 SINGLE_ADMIN_URL，并进入 /market_manager/merchants。
    """
    if backend == "single":
        admin_url = SINGLE_ADMIN_URL
        admin_user = SINGLE_ADMIN_USER
        admin_pass = SINGLE_ADMIN_PASS
        merchants_url = f"{SINGLE_ADMIN_ROOT}/market_manager/merchants"
    else:
        admin_url = BASE_ADMIN_URL
        admin_user = ADMIN_USER
        admin_pass = ADMIN_PASS
        merchants_url = f"{BASE_ADMIN_URL}/merchants"

    if not admin_url:
        raise Exception("商城后台 URL 未配置")
    if not admin_user or not admin_pass:
        raise Exception("商城后台账号或密码未配置")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-blink-features=AutomationControlled']
        )
        try:
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(20000)

            await _login_generic(page, admin_url, admin_user, admin_pass)

            if backend == "single":
                # 单笔商城已确认真实搜索字段是 q_username_eq。
                await page.goto(merchants_url, wait_until="domcontentloaded")
                search_input = await _first_visible(page, [
                    "#q_username_eq",
                    "input[name='q[username_eq]']",
                    "#q_username",
                    "input[name='q[username]']",
                    "input[name*='account']",
                    "input[type='search']",
                    "input[type='text']",
                ], timeout=8000)
                if not search_input:
                    raise Exception(f"单笔商城找不到商户搜索框；当前地址：{page.url}")

                await search_input.fill(account_name)
                search_btn = await _first_visible(page, [
                    "button:has-text('搜尋')",
                    "button:has-text('搜索')",
                    "input[type='submit']",
                    ".btn-primary",
                ], timeout=3000)
                if search_btn:
                    await search_btn.click()
                else:
                    await search_input.press("Enter")

                await page.wait_for_timeout(250)
                rows = page.locator("tbody tr")
                target_row = None
                for i in range(await rows.count()):
                    row = rows.nth(i)
                    try:
                        txt = _clean_text_value(await row.inner_text())
                        if account_name.lower() in txt.lower():
                            target_row = row
                            break
                    except Exception:
                        continue
                if target_row is None:
                    raise Exception(f"单笔商城找不到商户【{account_name}】")

                edit_link = target_row.locator("a[href$='/edit']").first
                await edit_link.wait_for(state="visible", timeout=10000)
                await edit_link.click()
                await page.wait_for_load_state("domcontentloaded")

            else:
                # 全部商城维持原本的搜索逻辑。
                await page.goto(merchants_url, wait_until="domcontentloaded")
                search_input = page.locator(
                    "input[name*='account'], #search_account, input[type='search'], input[type='text']"
                ).first
                await search_input.wait_for(state="visible", timeout=20000)
                await search_input.fill(account_name)
                search_btn = page.locator(
                    "button:has-text('搜尋'), button:has-text('搜索'), input[type='submit'], .btn-primary"
                ).first
                if await search_btn.is_visible():
                    await search_btn.click()
                else:
                    await search_input.press("Enter")
                await page.locator("tbody tr").first.wait_for(state="visible", timeout=20000)
                await page.locator("tbody tr").first.locator("a[href$='/edit']").click()
                await page.wait_for_load_state("domcontentloaded")

            shop_template = page.locator("#merchant_store_skin_type").first
            await shop_template.wait_for(state="visible", timeout=10000)
            try:
                await shop_template.select_option(label=new_skin)
            except Exception:
                try:
                    await shop_template.select_option(label=f"预设{new_skin}")
                except Exception:
                    # 最后尝试按 option 文字去掉“预设”后的匹配。
                    options = await shop_template.locator("option").evaluate_all(
                        "els => els.map(e => ({value:e.value,text:(e.textContent||'').trim()}))"
                    )
                    matched = next(
                        (x for x in options if x.get("text", "").replace("预设", "") == new_skin),
                        None
                    )
                    if not matched:
                        raise Exception(f"后台找不到商城界面【{new_skin}】")
                    await shop_template.select_option(value=matched["value"])

            submit = await _first_visible(page, [
                "input[name='commit'][value='送出']",
                "input[type='submit'][value='送出']",
                "input[name='commit']",
                "button[type='submit']",
            ], timeout=10000)
            if not submit:
                raise Exception("商城编辑页面找不到【送出】按钮")

            await submit.click()
            await page.wait_for_load_state("domcontentloaded")
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# 默认主按钮键盘
def build_main_keyboard(account: str, current_skin: str = "极速微商", backend: str = "all") -> InlineKeyboardMarkup:
    current_skin = current_skin.replace("预设", "")
    # callback 中明确记录后台类型，避免单笔商城误走全部商城后台。
    backend_key = "s" if backend == "single" else "a"
    buttons = [
        [InlineKeyboardButton(
            f"✨ 更改商城界面（当前{current_skin}）",
            callback_data=f"op:{backend_key}:{account}:{current_skin}"
        )]
    ]
    return InlineKeyboardMarkup(buttons)


# 展开风格选项键盘
def build_skin_options_keyboard(account: str, current_skin: str = "极速微商", backend: str = "all") -> InlineKeyboardMarkup:
    current_skin = current_skin.replace("预设", "")
    backend_key = "s" if backend == "single" else "a"
    buttons = [
        [
            InlineKeyboardButton("极速微商", callback_data=f"sk:{backend_key}:jisumeishang:{account}"),
            InlineKeyboardButton("七喵", callback_data=f"sk:{backend_key}:qimiao:{account}")
        ],
        [
            InlineKeyboardButton("柒月", callback_data=f"sk:{backend_key}:qiyue:{account}"),
            InlineKeyboardButton("音你而来", callback_data=f"sk:{backend_key}:yinnierlai:{account}")
        ],
        [
            InlineKeyboardButton("⬅️ 收起", callback_data=f"cl:{backend_key}:{account}:{current_skin}")
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
    # 有“单笔/订单号”或直接出现 UUID 订单号，都自动进入单笔商城。
    has_order_number = bool(_extract_order_numbers(user_text))
    if not any(k in user_text for k in trigger_keywords) and not has_order_number:
        return

    parsed_info, error_msg = parse_and_validate_text(user_text)

    if error_msg:
        await update.message.reply_text(error_msg, parse_mode="HTML", disable_web_page_preview=True)
        return

    is_single = bool(parsed_info.get("single_order_nos") or parsed_info.get("single_order_no"))

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



async def _jj_query_with_recovery(query_func, order_no, task_id, session):
    """查询阶段专用恢复：首次遇到非“找不到订单”异常时，重建 JJ Session 后重试一次。

    这是只读查询恢复，不用于充值/提现提交，避免表单已送出后重复制作。
    """
    try:
        return await query_func(order_no, task_id, session=session)
    except JJOrderNotFound:
        raise
    except Exception as first_error:
        _debug_log(f"[JJ] 查询异常，准备重建 Session 后重试一次：order={order_no}, error={first_error!r}")
        try:
            await session.reset(JJ_ADMIN_URL, JJ_ADMIN_USER, JJ_ADMIN_PASS, task_id=task_id)
        except Exception as reset_error:
            raise first_error from reset_error
        return await query_func(order_no, task_id, session=session)


# 建店 Worker 包装（含排队锁控制）
async def run_shop_worker(status_msg, parsed_info, task_id: str, is_single=False):
    try:
        async with BUILD_SHOP_SEMAPHORE:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel:{task_id}")]
            ])
            if is_single:
                await status_msg.edit_text(
                    "⏳ <b>已轮到当前单笔任务，正在自动建店中，请稍候...</b>",
                    reply_markup=keyboard, parse_mode="HTML"
                )
            else:
                await status_msg.edit_text(
                    "⏳ <b>已轮到当前任务，正在自动建店中，请稍候...</b>",
                    reply_markup=keyboard, parse_mode="HTML"
                )

            initial_skin = parsed_info.get("skin", "极速微商").replace("预设", "")

            if is_single:
                # 这里严格只调用已经验证成功的单笔建店流程。
                result_text, final_account = await _create_single_shop(parsed_info, task_id)

                # 建店成功后先明确回报，不让 JJ 查询覆盖/阻塞建店结果。
                await status_msg.edit_text(
                    result_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ 取消任务", callback_data=f"cancel:{task_id}")]
                    ]),
                    parse_mode="HTML", disable_web_page_preview=True
                )

                order_numbers = parsed_info.get("single_order_nos") or [parsed_info["single_order_no"]]
                expected_payment_account = _payment_account_from_info(parsed_info)
                recharge_results = []
                withdraw_results = []
                failed_pdd_results = []
                failed_outbound_results = []
                not_found_results = []
                payment_mismatch_results = []
                outbound_error_results = []
                recharge_error_results = []
                pdd_error_results = []
                withdraw_error_results = []
                # 同一单笔任务内复用 JJ 与单笔商城登录会话，减少重复启动浏览器/重复登录。
                jj_session = _ReusableBrowserSession(use_totp=True)
                single_session = _ReusableBrowserSession(use_totp=False)

                # 重要：绝大多数订单来自出货管理，所以每一笔都先查出货管理。
                # 只有出货管理完全找不到，才进入拼多多订单管理。
                for idx, order_no in enumerate(order_numbers, 1):
                    await status_msg.edit_text(
                        result_text +
                        f"\n\n⏳ 正在查询第 {idx}/{len(order_numbers)} 笔订单：<code>{html.escape(order_no)}</code>\n"
                        "查询顺序：出货管理 → 拼多多订单管理",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("❌ 取消任务", callback_data=f"cancel:{task_id}")]
                        ]),
                        parse_mode="HTML", disable_web_page_preview=True
                    )

                    # --------------------------------------------------
                    # A. 先查 JJ 出货管理
                    # --------------------------------------------------
                    try:
                        outbound_result = await _jj_query_with_recovery(_query_jj_order, order_no, task_id, jj_session)
                    except JJOrderNotFound as outbound_not_found:
                        # 只有“出货管理真正没有找到订单”才允许分流到拼多多。
                        _debug_log(f"[出货] 确认未找到订单，才分流 PDD: order={order_no}")
                        _debug_log(f"[出货] not-found detail: {outbound_not_found!r}")
                        outbound_result = None
                    except Exception as outbound_error:
                        # 出货管理已经进入查询/处理阶段后，任何异常都只影响当前订单。
                        # 特别是“收款帐户详情页读取失败”，绝不能误分流到 PDD，
                        # 但也不能中断后面的订单；当前订单记为异常后继续下一笔。
                        _debug_log(f"[出货] 查询/处理异常，当前订单记异常并继续下一笔，禁止分流 PDD: order={order_no}, repr={outbound_error!r}")
                        _debug_log(traceback.format_exc())
                        outbound_error_text = str(outbound_error) or repr(outbound_error)
                        outbound_error_results.append((order_no, outbound_error_text))
                        continue

                    if outbound_result is not None:
                        # 出货管理命中 = 商户充值流程。
                        # 成功订单有運單號/配送时间；失败订单没有这两项是正常的。
                        # 两种状态都必须先核对收款帐户「帳號」，核对后制作当前订单，随后继续下一笔。
                        actual_payment_account = outbound_result.get("payment_account", "")
                        if not _payment_account_matches(expected_payment_account, actual_payment_account):
                            # 收款号不符只记录当前订单，必须继续制作下一笔订单。
                            payment_mismatch_results.append({
                                "order_no": order_no,
                                "expected": expected_payment_account or "未读取",
                                "actual": actual_payment_account or "未读取",
                            })
                            _debug_log(
                                f"[出货] 收款号不符，跳过当前充值并继续下一笔: "
                                f"order={order_no}, expected={expected_payment_account!r}, actual={actual_payment_account!r}"
                            )
                            continue

                        try:
                            await status_msg.edit_text(
                                result_text +
                                f"\n\n⏳ 第 {idx}/{len(order_numbers)} 笔：<b>出货管理命中</b>\n"
                                f"订单状态：<b>{html.escape(outbound_result.get('status') or '未知')}</b>，收款号核对通过，正在制作商户充值...",
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("❌ 取消任务", callback_data=f"cancel:{task_id}")]
                                ]),
                                parse_mode="HTML", disable_web_page_preview=True
                            )
                            recharge_result = await _single_recharge(final_account, outbound_result, parsed_info, task_id, session=single_session)
                            recharge_results.append((outbound_result, recharge_result))
                            if outbound_result.get("status") == "失败":
                                failed_outbound_results.append(outbound_result)
                            # 每制作完一笔立即回报，然后才进入下一笔。
                            await status_msg.edit_text(
                                result_text +
                                f"\n\n✅ 第 {idx}/{len(order_numbers)} 笔订单制作完成：<code>{html.escape(order_no)}</code>"
                                f"\n类型：商户充值"
                                f"\n状态：{html.escape(outbound_result.get('status') or '未知')}"
                                "\n\n⏳ 准备进入下一笔订单...",
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("❌ 取消任务", callback_data=f"cancel:{task_id}")]
                                ]),
                                parse_mode="HTML", disable_web_page_preview=True
                            )
                        except Exception as recharge_error:
                            recharge_error_text = str(recharge_error) or repr(recharge_error)
                            recharge_error_results.append((order_no, recharge_error_text))
                            _debug_log(
                                f"[充值] 当前订单新增充值失败，继续下一笔: order={order_no}, repr={recharge_error!r}"
                            )
                            _debug_log(traceback.format_exc())
                            await status_msg.edit_text(
                                result_text +
                                f"\n\n⚠️ 第 {idx}/{len(order_numbers)} 笔订单充值失败，已继续下一笔"
                                f"\n订单号：<code>{html.escape(order_no)}</code>"
                                f"\n原因：{html.escape(recharge_error_text)}",
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("❌ 取消任务", callback_data=f"cancel:{task_id}")]
                                ]),
                                parse_mode="HTML", disable_web_page_preview=True
                            )
                            continue
                        continue

                    # --------------------------------------------------
                    # B. 出货管理没找到 → 查 JJ 拼多多订单管理
                    # --------------------------------------------------
                    try:
                        pdd_result = await _jj_query_with_recovery(_jj_query_pdd_order, order_no, task_id, jj_session)
                    except Exception as pdd_error:
                        # PDD 当前订单查询异常也不应阻断后面的订单。
                        _debug_log(f"[PDD] 查询异常，当前订单记异常并继续下一笔: order={order_no}, repr={pdd_error!r}")
                        _debug_log("[PDD] 完整 Traceback 开始")
                        _debug_log(traceback.format_exc())
                        _debug_log("[PDD] 完整 Traceback 结束")
                        pdd_error_text = str(pdd_error) or repr(pdd_error) or "未知异常（str 为空）"
                        pdd_error_results.append((order_no, pdd_error_text))
                        continue

                    if pdd_result is None:
                        not_found_results.append(order_no)
                        continue

                    # 拼多多命中后，不做收款号核对。
                    if pdd_result.get("status") == "失败":
                        failed_pdd_results.append(pdd_result)
                        continue

                    try:
                        await status_msg.edit_text(
                            result_text +
                            f"\n\n⏳ 第 {idx}/{len(order_numbers)} 笔：<b>拼多多订单管理命中</b>\n"
                            "订单状态：<b>成功</b>，正在制作商户提现...",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("❌ 取消任务", callback_data=f"cancel:{task_id}")]
                            ]),
                            parse_mode="HTML", disable_web_page_preview=True
                        )
                        withdraw_result = await _single_withdraw(final_account, pdd_result, task_id, session=single_session)
                        withdraw_results.append((pdd_result, withdraw_result))
                        # 每制作完一笔立即回报，然后才进入下一笔。
                        await status_msg.edit_text(
                            result_text +
                            f"\n\n✅ 第 {idx}/{len(order_numbers)} 笔订单制作完成：<code>{html.escape(order_no)}</code>"
                            "\n类型：商户提现"
                            "\n状态：成功"
                            "\n\n⏳ 准备进入下一笔订单...",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("❌ 取消任务", callback_data=f"cancel:{task_id}")]
                            ]),
                            parse_mode="HTML", disable_web_page_preview=True
                        )
                    except Exception as withdraw_error:
                        withdraw_error_text = str(withdraw_error) or repr(withdraw_error)
                        withdraw_error_results.append((order_no, withdraw_error_text))
                        _debug_log(
                            f"[提现] 当前订单新增提现失败，继续下一笔: order={order_no}, repr={withdraw_error!r}"
                        )
                        _debug_log(traceback.format_exc())
                        await status_msg.edit_text(
                            result_text +
                            f"\n\n⚠️ 第 {idx}/{len(order_numbers)} 笔订单提现失败，已继续下一笔"
                            f"\n订单号：<code>{html.escape(order_no)}</code>"
                            f"\n原因：{html.escape(withdraw_error_text)}",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("❌ 取消任务", callback_data=f"cancel:{task_id}")]
                            ]),
                            parse_mode="HTML", disable_web_page_preview=True
                        )
                        continue

                # 所有订单都按“命中页面决定制作类型”完成后，再统一汇总。
                lines = [result_text, ""]
                if recharge_results:
                    lines.append(f"商户充值：<b>{len(recharge_results)} 笔</b>")
                    for idx, (jj_result, recharge_result) in enumerate(recharge_results, 1):
                        lines.append(
                            f"充值 {idx}：<code>{html.escape(jj_result.get('order_no', ''))}</code> "
                            f"→ <b>{html.escape(recharge_result)}</b>"
                        )

                if withdraw_results:
                    lines.append(f"商户提现：<b>{len(withdraw_results)} 笔</b>")
                    for idx, (jj_result, withdraw_result) in enumerate(withdraw_results, 1):
                        lines.append(
                            f"提现 {idx}：<code>{html.escape(jj_result.get('order_no', ''))}</code> "
                            f"→ <b>{html.escape(withdraw_result)}</b>"
                        )

                if payment_mismatch_results:
                    lines.append("")
                    lines.append("❌ <b>收款号不符（这些订单未制作充值）：</b>")
                    for item in payment_mismatch_results:
                        lines.append(
                            f"订单号：<code>{html.escape(item['order_no'])}</code>"
                            f"\n　输入收款号：<code>{html.escape(item['expected'])}</code>"
                            f"\n　JJ收款号：<code>{html.escape(item['actual'])}</code>"
                        )

                if outbound_error_results:
                    lines.append("")
                    lines.append("⚠️ <b>出货管理处理异常（已跳过并继续下一笔）：</b>")
                    for order_no, error_text in outbound_error_results:
                        lines.append(
                            f"订单号：<code>{html.escape(order_no)}</code>"
                            f"\n　原因：<code>{html.escape(error_text[:500])}</code>"
                        )

                if recharge_error_results:
                    lines.append("")
                    lines.append("⚠️ <b>商户充值制作失败（已跳过并继续下一笔）：</b>")
                    for order_no, error_text in recharge_error_results:
                        lines.append(
                            f"订单号：<code>{html.escape(order_no)}</code>"
                            f"\n　原因：<code>{html.escape(error_text[:500])}</code>"
                        )

                if pdd_error_results:
                    lines.append("")
                    lines.append("⚠️ <b>拼多多查询异常（已跳过并继续下一笔）：</b>")
                    for order_no, error_text in pdd_error_results:
                        lines.append(
                            f"订单号：<code>{html.escape(order_no)}</code>"
                            f"\n　原因：<code>{html.escape(error_text[:500])}</code>"
                        )

                if withdraw_error_results:
                    lines.append("")
                    lines.append("⚠️ <b>商户提现制作失败（已跳过并继续下一笔）：</b>")
                    for order_no, error_text in withdraw_error_results:
                        lines.append(
                            f"订单号：<code>{html.escape(order_no)}</code>"
                            f"\n　原因：<code>{html.escape(error_text[:500])}</code>"
                        )

                if failed_outbound_results:
                    lines.append("")
                    lines.append("⚠️ <b>出货订单状态为失败（已按失败订单流程制作充值）：</b>")
                    for jj_result in failed_outbound_results:
                        lines.append(f"<code>{html.escape(jj_result.get('order_no', '') or '未知订单号')}</code>")

                if failed_pdd_results:
                    lines.append("")
                    lines.append("⚠️ <b>拼多多订单失败：</b>")
                    for jj_result in failed_pdd_results:
                        lines.append(f"<code>{html.escape(jj_result.get('order_no', '') or '未知订单号')}</code>")

                if not_found_results:
                    lines.append("")
                    lines.append("⚠️ <b>订单未找到：</b>")
                    for order_no in not_found_results:
                        lines.append(f"<code>{html.escape(order_no)}</code>")

                await status_msg.edit_text(
                    "\n".join(lines),
                    reply_markup=build_main_keyboard(final_account, initial_skin, backend="single"),
                    parse_mode="HTML", disable_web_page_preview=True
                )
                await jj_session.close()
                await single_session.close()
            else:
                # 全部商城完全沿用原本已经跑通的流程。
                result_text, final_account = await create_and_setup_shop(parsed_info, task_id)
                await status_msg.edit_text(
                    result_text,
                    reply_markup=build_main_keyboard(final_account, initial_skin),
                    parse_mode="HTML", disable_web_page_preview=True
                )

    except asyncio.CancelledError:
        for _session_name in ("jj_session", "single_session"):
            _session = locals().get(_session_name)
            if _session is not None:
                try:
                    await _session.close()
                except Exception:
                    pass
        try:
            await status_msg.edit_text("🛑 <b>已取消建店！</b>", parse_mode="HTML")
        except Exception:
            pass
    except Exception as e:
        for _session_name in ("jj_session", "single_session"):
            _session = locals().get(_session_name)
            if _session is not None:
                try:
                    await _session.close()
                except Exception:
                    pass
        safe_err = html.escape(str(e))
        try:
            await status_msg.edit_text(
                f"❌ 建店出现错误: {safe_err}",
                parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception:
            pass
    finally:
        ACTIVE_TASKS.pop(task_id, None)


# 5. 回调事件处理
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    click_user_id = query.from_user.id

    # Telegram 的 callback query 必须尽快 answer。若先做网页操作再 answer，
    # 网页操作稍慢时 Telegram 会返回：Query is too old and response timeout expired。
    # 因此所有普通按钮在进入分支前先立即确认一次，后面不再重复 answer。
    if data != "ignore":
        try:
            await query.answer()
        except Exception:
            pass

    if data == "ignore":
        try:
            await query.answer("⏳ 正在修改界面中，请勿重复点击...", show_alert=False)
        except Exception:
            pass
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
        # 格式：op:<a/s>:<account>:<current_skin>
        try:
            _, backend_key, account, current_skin = data.split(":", 3)
            backend = "single" if backend_key == "s" else "all"
            keyboard = build_skin_options_keyboard(account, current_skin, backend=backend)
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception as e:
            try:
                await query.answer("⚠️ 打开界面选择失败，请再点一次。", show_alert=True)
            except Exception:
                pass

    elif data.startswith("cl:"):
        # 格式：cl:<a/s>:<account>:<current_skin>
        try:
            _, backend_key, account, current_skin = data.split(":", 3)
            backend = "single" if backend_key == "s" else "all"
            keyboard = build_main_keyboard(account, current_skin, backend=backend)
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception as e:
            pass

    elif data.startswith("sk:"):
        # 格式：sk:<a/s>:<skin_key>:<account>
        _, backend_key, skin_key, account = data.split(":", 3)
        backend = "single" if backend_key == "s" else "all"
        new_skin_name = SKIN_OPTIONS.get(skin_key, "极速微商")

        # callback query 已在函数开头立即 answer，这里不再重复 answer。

        # 切换按钮为防重复点击状态
        loading_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⏳ 正在切换为【{new_skin_name}】...", callback_data="ignore")]
        ])
        await query.edit_message_reply_markup(reply_markup=loading_keyboard)

        try:
            await update_shop_skin(account, new_skin_name, backend=backend)
            keyboard = build_main_keyboard(account, new_skin_name, backend=backend)
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception as e:
            keyboard = build_skin_options_keyboard(account, current_skin=new_skin_name, backend=backend)
            await query.edit_message_reply_markup(reply_markup=keyboard)
            try:
                await query.answer(f"⚠️ 切换失败：{str(e)[:180]}", show_alert=True)
            except Exception:
                pass


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
