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

# 【建店专用排队锁】：同时只允许 1 个建店任务在后台运行，后续建店请求自动排队
BUILD_SHOP_SEMAPHORE = asyncio.Semaphore(1)

# 商城界面选项（与后台对应）
SKIN_OPTIONS = {
    "jisumeishang": "极速微商",
    "qimiao": "七喵",
    "qiyue": "柒月",
    "yinnierlai": "音你而来"
}


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

    if errors:
        error_summary = "❌ <b>建店失败！检测到以下输入错误：</b>\n\n" + "\n".join(errors)
        return None, error_summary

    return info, ""



# 3. 环境变量：单笔商城 / JJ 订单后台
SINGLE_ADMIN_USER = os.environ.get("SINGLE_ADMIN_USER", "").strip()
SINGLE_ADMIN_PASS = os.environ.get("SINGLE_ADMIN_PASS", "").strip()

raw_single_admin_url = os.environ.get("SINGLE_ADMIN_URL", "").strip()
match = re.search(r'https?://[^\s\]\)\>\"\']+', raw_single_admin_url)
SINGLE_ADMIN_URL = match.group(0).rstrip('/') if match else raw_single_admin_url.rstrip('/')

JJ_ADMIN_USER = os.environ.get("JJ_ADMIN_USER", "").strip()
JJ_ADMIN_PASS = os.environ.get("JJ_ADMIN_PASS", "").strip()
JJ_2FA_SECRET = os.environ.get("JJ_2FA_SECRET", "").replace(" ", "").strip()

raw_jj_admin_url = os.environ.get("JJ_ADMIN_URL", "").strip()
match = re.search(r'https?://[^\s\]\)\>\"\']+', raw_jj_admin_url)
JJ_ADMIN_URL = match.group(0).rstrip('/') if match else raw_jj_admin_url.rstrip('/')

# 运行时导入，避免原有环境没有 pyotp 时影响全部商城模块加载
try:
    import pyotp
except ImportError:
    pyotp = None


# 4. 重新定义解析器：在原有字段基础上可靠提取“单笔”
_ORIGINAL_PARSE_AND_VALIDATE_TEXT = parse_and_validate_text

def parse_and_validate_text(text: str) -> tuple[dict, str]:
    info, error = _ORIGINAL_PARSE_AND_VALIDATE_TEXT(text)

    # 单笔字段单独解析，不让原有的字段解析逻辑干扰。
    # 同时支持中英文冒号、全角冒号、不同空白、单笔/單筆。
    clean = re.sub(r'mailto:', '', text or '', flags=re.IGNORECASE)
    clean = re.sub(r'<[^>]+>', '', clean)

    single_match = re.search(
        r'(?im)^[ \t]*(?:单笔|單筆)[ \t]*[:：][ \t]*(.+?)[ \t]*$',
        clean
    )
    if single_match:
        single_order_no = single_match.group(1).strip()
        if single_order_no:
            if info is None:
                # 原解析失败时仍保持原错误，不在这里吞掉校验错误。
                return info, error
            info["single_order_no"] = single_order_no

    return info, error


def _label_locator(page, labels: list[str]):
    """用 label/文本做多重兜底定位，降低后台轻微改版的影响。"""
    locators = []
    for label in labels:
        locators.extend([
            page.get_by_label(label, exact=False),
            page.locator(f"label:has-text('{label}')").locator("xpath=following::*[self::input or self::textarea or self::select][1]"),
        ])
    for loc in locators:
        try:
            if loc.count() > 0:
                return loc.first
        except Exception:
            pass
    return page.locator("input").last


async def _first_visible(page, selectors: list[str], timeout=5000):
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            await loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


async def _login_admin(page, base_url: str, username: str, password: str, timeout=20000):
    await page.goto(base_url, wait_until="domcontentloaded")
    user_input = await _first_visible(page, [
        "#admin_user_email", "#user_email",
        "input[type='email']",
        "input[name*='email']",
        "input[name*='login']",
        "input[name*='username']",
        "input[type='text']"
    ], timeout)

    if not user_input:
        raise Exception(f"无法找到后台登录账号框！标题：{await page.title()}，地址：{page.url}")

    await user_input.fill(username)

    pass_input = await _first_visible(page, [
        "#admin_user_password", "#user_password",
        "input[type='password']"
    ], timeout)

    if not pass_input:
        raise Exception("无法找到后台密码框！")

    await pass_input.fill(password)

    submit_btn = await _first_visible(page, [
        "input[type='submit']",
        "button[type='submit']",
        "input[name='commit']",
        "button:has-text('登录')",
        "button:has-text('登入')"
    ], timeout)

    if not submit_btn:
        raise Exception("无法找到后台登录按钮！")

    await submit_btn.click()
    await page.wait_for_load_state("domcontentloaded")


async def _single_search_account(page, base_url: str, account: str):
    # 优先使用常见商户列表地址；如果站点当前地址结构不同，则退回站内导航。
    candidates = [
        f"{base_url}/merchants",
        f"{base_url}/market_managers",
        f"{base_url}/market_managers/merchants",
    ]

    last_error = None
    for url in candidates:
        try:
            await page.goto(url, wait_until="domcontentloaded")
            search_input = await _first_visible(page, [
                "input[name*='account']",
                "#search_account",
                "input[type='search']",
                "input[type='text']"
            ], 8000)
            if not search_input:
                continue

            await search_input.fill(account)

            search_btn = await _first_visible(page, [
                "button:has-text('搜尋')",
                "button:has-text('搜索')",
                "input[type='submit']",
                ".btn-primary"
            ], 3000)

            if search_btn:
                await search_btn.click()
            else:
                await search_input.press("Enter")

            await page.wait_for_timeout(700)
            rows = page.locator("tbody tr")
            if await rows.count() > 0:
                return rows.first
        except Exception as e:
            last_error = e

    raise Exception(f"单笔商城无法找到商户【{account}】。{last_error or ''}")


async def _single_create_shop(info: dict, task_id: str) -> tuple[str, str]:
    if not SINGLE_ADMIN_URL:
        raise Exception("未检测到环境变量 SINGLE_ADMIN_URL！")
    if not SINGLE_ADMIN_USER or not SINGLE_ADMIN_PASS:
        raise Exception("未检测到 SINGLE_ADMIN_USER / SINGLE_ADMIN_PASS！")

    base_account = info["account"]
    target_skin = info.get("skin", "极速微商").replace("预设", "")
    suffix_num = 0
    final_account = base_account

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"]
        )
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/122.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            page.set_default_timeout(20000)

            if task_id in ACTIVE_TASKS:
                ACTIVE_TASKS[task_id]["page"] = page

            await _login_admin(page, SINGLE_ADMIN_URL, SINGLE_ADMIN_USER, SINGLE_ADMIN_PASS)

            # 建店：优先沿用与全部商城相同的 merchants/new 路径。
            while True:
                current_account = base_account if suffix_num == 0 else f"{base_account}{suffix_num:02d}"

                new_urls = [
                    f"{SINGLE_ADMIN_URL}/merchants/new",
                    f"{SINGLE_ADMIN_URL}/market_managers/merchants/new",
                ]
                opened = False
                for new_url in new_urls:
                    try:
                        await page.goto(new_url, wait_until="domcontentloaded")
                        username_input = await _first_visible(page, [
                            "#merchant_username",
                            "input[name*='username']",
                            "input[name*='account']"
                        ], 8000)
                        if username_input:
                            opened = True
                            break
                    except Exception:
                        continue

                if not opened:
                    raise Exception("单笔商城找不到「建立店铺」页面或账号输入框。")

                await username_input.fill(current_account)

                for sel in ["#merchant_password", "input[name='merchant[password]']"]:
                    loc = page.locator(sel).first
                    try:
                        if await loc.is_visible():
                            await loc.fill("a12345")
                            break
                    except Exception:
                        pass

                for sel in ["#merchant_password_confirmation", "input[name*='password_confirmation']"]:
                    loc = page.locator(sel).first
                    try:
                        if await loc.is_visible():
                            await loc.fill("a12345")
                            break
                    except Exception:
                        pass

                # Sprite 平台默认 JJ。
                platform = page.locator("#merchant_sprite_platform").first
                try:
                    if await platform.is_visible():
                        try:
                            await platform.select_option(label="jj")
                        except Exception:
                            await platform.select_option(value="jj")
                except Exception:
                    pass

                for sel, value in [
                    ("#merchant_account_name", info.get("name", "")),
                    ("#merchant_phone", info.get("phone", "")),
                ]:
                    loc = page.locator(sel).first
                    try:
                        if await loc.is_visible():
                            await loc.fill(value)
                    except Exception:
                        pass

                info_type = info.get("type", "alipay")
                default_num = "6226220809397366"

                bank_name_input = page.locator(
                    "#merchant_bank_accounts_attributes_0_bank_name, input[id$='_bank_name']"
                ).first
                branch_name_input = page.locator(
                    "#merchant_bank_accounts_attributes_0_branch_name, input[id$='_branch_name']"
                ).first
                card_no_input = page.locator(
                    "#merchant_bank_accounts_attributes_0_account_no, input[id$='_account_no']"
                ).first

                if info_type == "bank":
                    values = [
                        (bank_name_input, info.get("bank_name", "")),
                        (branch_name_input, info.get("branch_name", "")),
                        (card_no_input, info.get("bank_account", "")),
                    ]
                else:
                    values = [
                        (bank_name_input, default_num),
                        (branch_name_input, default_num),
                        (card_no_input, default_num),
                    ]

                for loc, value in values:
                    try:
                        if await loc.is_visible():
                            await loc.fill(value)
                    except Exception:
                        pass

                alipay_input = page.locator("#merchant_alipay_accounts_attributes_0_account_name").first
                try:
                    if await alipay_input.is_visible():
                        await alipay_input.fill(info.get("alipay_account", "") if info_type == "alipay" else "")
                except Exception:
                    pass

                ecny_input = page.locator("#merchant_ecny_accounts_attributes_0_account_name").first
                try:
                    if await ecny_input.is_visible():
                        await ecny_input.fill(info.get("digital_account", "") if info_type == "digital_wallet" else "")
                except Exception:
                    pass

                skin = page.locator("#merchant_store_skin_type").first
                try:
                    if await skin.is_visible():
                        try:
                            await skin.select_option(label=target_skin)
                        except Exception:
                            await skin.select_option(index=1)
                except Exception:
                    pass

                submit = await _first_visible(page, [
                    "input[name='commit'][value='送出']",
                    "input[type='submit']",
                    "button[type='submit']"
                ], 8000)
                if not submit:
                    raise Exception("单笔商城找不到建店提交按钮。")

                await submit.click()
                await page.wait_for_load_state("domcontentloaded")

                body_text = await page.locator("body").inner_text()
                if any(x in body_text for x in ["已经被使用", "已經被使用", "已被使用", "已经存在", "已經存在"]):
                    suffix_num += 1
                    continue

                final_account = current_account
                break

            # 查询刚创建的店铺，取得店铺网址。
            row = await _single_search_account(page, SINGLE_ADMIN_URL, final_account)
            cells = row.locator("td")
            shop_url = ""
            for i in range(await cells.count()):
                txt = (await cells.nth(i).inner_text()).strip()
                if txt.startswith("http://") or txt.startswith("https://"):
                    shop_url = txt
                    break
            if not shop_url:
                shop_url = f"{SINGLE_ADMIN_URL}/merchants"

            # 商品 60。
            try:
                items_link = row.locator("a[href$='/items']").first
                await items_link.click()
                await page.wait_for_load_state("domcontentloaded")
                new_item = await _first_visible(page, [
                    "a[href*='/items/new']",
                    "a:has-text('導入商品')",
                    "a:has-text('导入商品')"
                ], 8000)
                if new_item:
                    await new_item.click()
                    await page.wait_for_load_state("domcontentloaded")
                    count_input = await _first_visible(page, [
                        "#count_of_items",
                        "input[name='count_of_items']"
                    ], 8000)
                    if count_input:
                        await count_input.fill("60")
                        submit = await _first_visible(page, [
                            "input[name='commit']",
                            "input[value='送出']",
                            "button[type='submit']"
                        ], 5000)
                        if submit:
                            await submit.click()
                            await page.wait_for_load_state("domcontentloaded")
            except Exception as e:
                print(f"⚠️ [单笔商城商品60] {e}")

            # 非银行付款方式：删除默认银行占位符。
            if info_type != "bank":
                try:
                    row = await _single_search_account(page, SINGLE_ADMIN_URL, final_account)
                    edit_link = row.locator("a[href$='/edit']").first
                    await edit_link.click()
                    await page.wait_for_load_state("domcontentloaded")

                    bank_section = page.locator(
                        ".nested-fields, div:has(#merchant_bank_accounts_attributes_0_account_no)"
                    ).first
                    remove_btn = bank_section.locator(
                        "a.remove_fields, a:has-text('移除'), a:has-text('删除')"
                    ).first

                    if not await remove_btn.is_visible():
                        remove_btn = page.locator(
                            "a.remove_fields, a:has-text('移除'), a:has-text('删除')"
                        ).first

                    if await remove_btn.is_visible():
                        await remove_btn.click()
                        submit = await _first_visible(page, [
                            "input[name='commit'][value='送出']",
                            "input[type='submit']"
                        ], 5000)
                        if submit:
                            await submit.click()
                            await page.wait_for_load_state("domcontentloaded")
                except Exception as e:
                    print(f"⚠️ [单笔商城移除银行占位符] {e}")

            # 单笔商城流程到这里不做 6000 出货、不做 6000 提现。
            return (
                "✅ <b>单笔商城建店完成！</b>\n\n"
                f"店铺网址 : <code>{html.escape(shop_url)}</code>\n"
                f"登入帐号 : <code>{html.escape(final_account)}</code>\n"
                "登入密码 : <code>a12345</code>\n\n"
                "⏳ 正在查询 JJ 订单并准备充值资料……"
            ), final_account

        except PlaywrightTimeoutError:
            raise Exception("单笔商城建店关键流程超时，请检查后台响应或 selector。")
        finally:
            try:
                await browser.close()
            except Exception:
                pass


def _normalize_order_text(value: str) -> str:
    return re.sub(r'\s+', ' ', (value or '').strip())


def _looks_like_person_name(value: str) -> bool:
    """
    实名判断：
    - 空白、纯数字 → 管理员代收
    - 含明显随机 ID/UUID/大量符号 → 管理员代收
    - 中文姓名、常规英文字母姓名 → 接受
    """
    value = _normalize_order_text(value)
    if not value:
        return False
    if re.fullmatch(r'\d+', value):
        return False
    if re.search(r'[0-9]{4,}', value):
        return False
    if re.search(r'[{}[\]<>@#$%^*_+=|\\/]', value):
        return False

    chinese = re.sub(r'[\s·•]', '', value)
    if re.fullmatch(r'[\u4e00-\u9fff]{2,6}', chinese):
        return True

    english = re.sub(r"[\s.'-]", "", value)
    if re.fullmatch(r"[A-Za-z]{2,30}", english):
        return True

    # 混合数字/字母通常不是正常实名。
    if re.search(r'\d', value):
        return False

    return False


def _admin_receiver_name(jj_name: str) -> str:
    return _normalize_order_text(jj_name) if _looks_like_person_name(jj_name) else "管理员代收"


def _parse_datetime(value: str):
    from datetime import datetime
    value = _normalize_order_text(value)
    formats = [
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _random_delivery_time(created_at: str) -> str:
    from datetime import timedelta
    import random

    dt = _parse_datetime(created_at)
    if not dt:
        raise Exception(f"JJ 建立时间无法解析：{created_at}")

    day_offset = random.choice([1, 2])
    hour = random.randint(8, 18)
    minute = random.randint(0, 59)

    # 18:00 不能再加随机分钟。
    if hour == 18:
        minute = 0

    result = dt + timedelta(days=day_offset)
    result = result.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return result.strftime("%Y/%m/%d %H:%M")


async def _jj_login(page):
    if not JJ_ADMIN_URL:
        raise Exception("未检测到环境变量 JJ_ADMIN_URL！")
    if not JJ_ADMIN_USER or not JJ_ADMIN_PASS:
        raise Exception("未检测到 JJ_ADMIN_USER / JJ_ADMIN_PASS！")
    if not JJ_2FA_SECRET:
        raise Exception("未检测到 JJ_2FA_SECRET！")
    if pyotp is None:
        raise Exception("缺少 pyotp，请确认 requirements.txt 已安装 pyotp。")

    await page.goto(JJ_ADMIN_URL, wait_until="domcontentloaded")

    user_input = await _first_visible(page, [
        "input[type='email']",
        "input[name*='email']",
        "input[name*='login']",
        "input[name*='username']",
        "input[type='text']"
    ], 20000)
    if not user_input:
        raise Exception("JJ 找不到账号输入框。")
    await user_input.fill(JJ_ADMIN_USER)

    pass_input = await _first_visible(page, [
        "input[type='password']",
        "input[name*='password']"
    ], 10000)
    if not pass_input:
        raise Exception("JJ 找不到密码输入框。")
    await pass_input.fill(JJ_ADMIN_PASS)

    totp_input = await _first_visible(page, [
        "input[name*='otp']",
        "input[name*='totp']",
        "input[name*='code']",
        "input[placeholder*='Google']",
        "input[placeholder*='验证码']",
        "input[placeholder*='驗證']"
    ], 5000)
    if not totp_input:
        # 最后的 6 位数字输入框兜底
        candidates = page.locator("input")
        for i in range(await candidates.count()):
            loc = candidates.nth(i)
            try:
                typ = (await loc.get_attribute("type") or "").lower()
                name = (await loc.get_attribute("name") or "").lower()
                if typ in ("text", "tel", "number") and any(
                    k in name for k in ["otp", "totp", "code", "google", "auth"]
                ):
                    totp_input = loc
                    break
            except Exception:
                pass

    if not totp_input:
        raise Exception("JJ 找不到 Google Authenticator 验证码输入框。")

    totp = pyotp.TOTP(JJ_2FA_SECRET).now()
    await totp_input.fill(totp)

    submit = await _first_visible(page, [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('登录')",
        "button:has-text('登入')"
    ], 10000)
    if not submit:
        raise Exception("JJ 找不到登录按钮。")

    await submit.click()
    await page.wait_for_load_state("domcontentloaded")


async def _jj_set_unlock_and_date(page, created_date_text: str):
    # 出货管理页面
    candidates = [
        f"{JJ_ADMIN_URL}/admin/guest_payment_orders",
        f"{JJ_ADMIN_URL}/admin/guest_payment_orders?..."
    ]

    opened = False
    for url in candidates:
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if "出货管理" in await page.locator("body").inner_text():
                opened = True
                break
        except Exception:
            continue

    if not opened:
        # 如果默认登录后已经在出货管理，则继续。
        body = await page.locator("body").inner_text()
        if "出货管理" not in body and "出貨管理" not in body:
            raise Exception("JJ 无法进入「出货管理」页面。")

    # 暗锁：用户截图显示元素有 .unlock-btn。
    unlock = page.locator(".unlock-btn").first
    try:
        if await unlock.count() > 0 and await unlock.is_visible():
            classes = await unlock.get_attribute("class") or ""
            # 看到 lock/锁定状态时点击；已 unlock 时不重复点击。
            if "unlock" not in classes or "fa-unlock" not in classes:
                await unlock.click()
            else:
                # 截图中初始按钮可能是 lock 图标，点击后 class 才变成 unlock。
                title = await unlock.get_attribute("title") or ""
                aria = await unlock.get_attribute("aria-label") or ""
                text = f"{title} {aria}".lower()
                if "lock" in text and "unlock" not in text:
                    await unlock.click()
        else:
            # 兜底：寻找带 unlock-btn 的父级可点击元素。
            fallback = page.locator("i.unlock-btn").first
            if await fallback.count() > 0:
                await fallback.click()
    except Exception as e:
        print(f"⚠️ JJ 暗锁点击检查：{e}")

    # 给日期控件一点反应时间。
    await page.wait_for_timeout(300)

    # 选择「建立日期」单选项。
    for loc in [
        page.get_by_text("建立日期", exact=True).first,
        page.locator("label:has-text('建立日期')").first,
    ]:
        try:
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click()
                break
        except Exception:
            pass

    # 日期范围：现在往前一年。
    from datetime import datetime, timedelta
    now = datetime.now()
    start = now - timedelta(days=365)
    start_str = start.strftime("%Y/%m/%d 00:00")
    end_str = now.strftime("%Y/%m/%d 23:59")

    date_inputs = page.locator(
        "input[name*='created'], input[id*='created'], "
        "input[name*='start'], input[name*='end'], "
        "input[type='datetime-local'], input[type='text']"
    )

    visible_inputs = []
    for i in range(await date_inputs.count()):
        loc = date_inputs.nth(i)
        try:
            if await loc.is_visible():
                visible_inputs.append(loc)
        except Exception:
            pass

    # 尽量按建立日期区域中的两个输入框处理。
    date_box = page.locator("label:has-text('建立日期')").locator("xpath=..").first
    try:
        scoped = date_box.locator("input")
        if await scoped.count() >= 2:
            visible_inputs = [scoped.nth(0), scoped.nth(1)]
    except Exception:
        pass

    if len(visible_inputs) >= 2:
        for loc, value in [(visible_inputs[0], start_str), (visible_inputs[1], end_str)]:
            try:
                await loc.fill(value)
            except Exception:
                # datetime-local 控件使用 ISO 格式。
                try:
                    iso = value.replace("/", "-").replace(" ", "T")
                    await loc.fill(iso)
                except Exception:
                    pass


async def _jj_search_order(page, platform_order_no: str) -> dict | None:
    """
    先搜平台订单号；无结果后再搜其他订单号。
    返回：
      status, platform_order_no, other_order_no, shipment_no,
      recipient_name, amount, delivery_time, created_at
    """
    async def do_search(field_kind: str):
        if field_kind == "platform":
            input_loc = await _first_visible(page, [
                "input[name*='platform_order']",
                "input[id*='platform_order']",
                "input[placeholder*='平台订单号']",
                "input[placeholder*='平台訂單號']"
            ], 5000)
            if not input_loc:
                input_loc = await _label_locator(page, ["平台订单号", "平台訂單號"])
        else:
            input_loc = await _first_visible(page, [
                "input[name*='other_order']",
                "input[id*='other_order']",
                "input[placeholder*='其他订单号']",
                "input[placeholder*='其他訂單號']"
            ], 5000)
            if not input_loc:
                input_loc = await _label_locator(page, ["其他订单号", "其他訂單號"])

        if not input_loc:
            return None

        await input_loc.fill(platform_order_no)

        search_btn = await _first_visible(page, [
            "button:has-text('搜尋')",
            "button:has-text('搜索')",
            "input[type='submit'][value*='搜']",
            "button[type='submit']",
            ".btn-primary"
        ], 5000)
        if not search_btn:
            raise Exception("JJ 找不到搜尋按钮。")

        await search_btn.click()
        await page.wait_for_timeout(800)

        rows = page.locator("tbody tr")
        if await rows.count() == 0:
            return None

        # 过滤掉“没有数据”的空行。
        for i in range(await rows.count()):
            row = rows.nth(i)
            txt = _normalize_order_text(await row.inner_text())
            if txt and not any(x in txt for x in ["没有资料", "沒有資料", "无数据", "無資料"]):
                return await _parse_jj_row(row)
        return None

    result = await do_search("platform")
    if result:
        return result

    return await do_search("other")


async def _parse_jj_row(row) -> dict:
    cells = row.locator("td")
    texts = []
    for i in range(await cells.count()):
        texts.append(_normalize_order_text(await cells.nth(i).inner_text()))

    full = " | ".join(texts)

    def find_after(labels):
        for label in labels:
            m = re.search(re.escape(label) + r"\s*[:：]?\s*([^|]+)", full, re.I)
            if m:
                return _normalize_order_text(m.group(1))
        return ""

    # 截图表格的字段顺序：
    # 提交时间、完成时间、订单号、平台会员/通道/拼多多、采购方、商户会员、
    # 出货平台、交易金额、金流、图片、等待时长、到期时间、异常回报、状态。
    status = ""
    for t in texts:
        if "成功" in t:
            status = "success"
            break
        if "失败" in t:
            status = "failed"
            break

    # 订单号单元格可能同时包含平台订单号/其他订单号，全部保留以便日志定位。
    order_blob = next((t for t in texts if "订单" in t or "訂單" in t), "")
    amount = ""
    for t in texts:
        m = re.search(r'(\d+(?:\.\d+)?)\s*(?:CNY|CN¥|元)', t, re.I)
        if m:
            amount = m.group(1)
            break

    # 运输/实名在不同 JJ 版本可能是详情文本、展开字段或链接。
    shipment_no = find_after(["货运", "貨運", "运单号", "運單號", "物流单号", "物流單號"])
    recipient_name = find_after(["实名", "實名", "收件人姓名", "收件人"])
    created_at = texts[0] if texts else ""
    delivery_time = find_after(["配送时间", "配送時間"])

    return {
        "status": status,
        "order_blob": order_blob,
        "shipment_no": shipment_no,
        "recipient_name": recipient_name,
        "amount": amount,
        "delivery_time": delivery_time,
        "created_at": created_at,
        "raw_text": full,
    }


async def _jj_query_order(platform_order_no: str) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"]
        )
        try:
            page = await browser.new_page()
            page.set_default_timeout(20000)

            await _jj_login(page)
            await _jj_set_unlock_and_date(page, "")
            result = await _jj_search_order(page, platform_order_no)

            if not result:
                raise Exception(f"JJ 找不到订单：{platform_order_no}")

            return result
        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def _single_recharge(account: str, jj: dict):
    if not SINGLE_ADMIN_URL:
        raise Exception("未检测到 SINGLE_ADMIN_URL！")
    if not SINGLE_ADMIN_USER or not SINGLE_ADMIN_PASS:
        raise Exception("未检测到 SINGLE_ADMIN_USER / SINGLE_ADMIN_PASS！")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"]
        )
        try:
            page = await browser.new_page()
            page.set_default_timeout(20000)
            await _login_admin(page, SINGLE_ADMIN_URL, SINGLE_ADMIN_USER, SINGLE_ADMIN_PASS)

            # 充值管理入口。
            recharge_urls = [
                f"{SINGLE_ADMIN_URL}/deposit_orders/new",
                f"{SINGLE_ADMIN_URL}/deposits/new",
                f"{SINGLE_ADMIN_URL}/market_managers/deposit_orders/new",
            ]
            opened = False
            for url in recharge_urls:
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                    if await page.locator(
                        "input[name*='deposit_order'], #deposit_order_shipment_no"
                    ).count() > 0 or "充值" in await page.locator("body").inner_text():
                        opened = True
                        break
                except Exception:
                    continue

            if not opened:
                # 从菜单点击「商户充值管理」/「充值管理」。
                menu = page.get_by_text("商户充值管理", exact=False).first
                if await menu.count() == 0:
                    menu = page.get_by_text("充值管理", exact=False).first
                if await menu.count() > 0:
                    await menu.click()
                    await page.wait_for_load_state("domcontentloaded")

                add = await _first_visible(page, [
                    "a:has-text('充值')",
                    "a[href*='deposit']",
                    "a[href*='recharge']"
                ], 8000)
                if add:
                    await add.click()
                    await page.wait_for_load_state("domcontentloaded")
                    opened = True

            if not opened:
                raise Exception("单笔商城找不到充值页面。")

            # 商户：选择刚创建的商户。
            merchant = await _first_visible(page, [
                "#deposit_order_merchant_id",
                "select[name*='merchant']",
                "select[id*='merchant']"
            ], 8000)
            if merchant:
                try:
                    await merchant.select_option(label=account)
                except Exception:
                    options = merchant.locator("option")
                    matched = False
                    for i in range(await options.count()):
                        txt = _normalize_order_text(await options.nth(i).inner_text())
                        val = await options.nth(i).get_attribute("value")
                        if account.lower() in txt.lower() or account.lower() == (val or "").lower():
                            if val:
                                await merchant.select_option(value=val)
                                matched = True
                                break
                    if not matched:
                        raise Exception(f"充值商户下拉找不到【{account}】。")

            # 银行帐号：自动跳转，不填。
            # 收件人资讯：随便选一个现有选项。
            recipient_info = await _first_visible(page, [
                "#deposit_order_recipient_info_id",
                "select[name*='recipient_info']",
                "select[id*='recipient_info']"
            ], 5000)
            if recipient_info:
                try:
                    options = recipient_info.locator("option")
                    for i in range(await options.count()):
                        value = await options.nth(i).get_attribute("value")
                        disabled = await options.nth(i).is_disabled()
                        if value and not disabled:
                            await recipient_info.select_option(value=value)
                            break
                except Exception:
                    pass

            # 买家留言保持空白。
            buyer_comment = page.locator(
                "#deposit_order_buyer_comment, textarea[name*='buyer_comment']"
            ).first
            try:
                if await buyer_comment.is_visible():
                    await buyer_comment.fill("")
            except Exception:
                pass

            status = jj.get("status")
            if status not in ("success", "failed"):
                raise Exception("JJ 订单状态无法判断。")

            # 运单号：失败订单没有，不填；成功才填。
            shipment_no = jj.get("shipment_no", "")
            if status == "success" and shipment_no:
                shipment = page.locator(
                    "#deposit_order_shipment_no, "
                    "input[name*='shipment_no'], input[name*='waybill']"
                ).first
                try:
                    if await shipment.is_visible():
                        await shipment.fill(shipment_no)
                except Exception:
                    pass

            # 收件人姓名：空白或异常值自动使用管理员代收。
            recipient = _admin_receiver_name(jj.get("recipient_name", ""))
            recipient_input = page.locator(
                "#deposit_order_recipient_name, "
                "input[name*='recipient_name']"
            ).first
            if await recipient_input.count() > 0:
                await recipient_input.fill(recipient)

            # 金额：JJ 交易金额。
            amount = jj.get("amount", "")
            if not amount:
                raise Exception("JJ 订单没有读取到交易金额。")
            amount_input = page.locator(
                "#deposit_order_total_amount, "
                "input[name*='total_amount'], input[name*='amount']"
            ).first
            if await amount_input.count() > 0:
                await amount_input.fill(amount)

            # 配送时间：只有成功订单才生成；建立时间后 1～2 天，08:00～18:00。
            if status == "success":
                delivery = jj.get("delivery_time", "")
                if not delivery:
                    delivery = _random_delivery_time(jj.get("created_at", ""))
                delivery_input = page.locator(
                    "#deposit_order_delivery_at, "
                    "#deposit_order_delivery_time, "
                    "input[name*='delivery_at'], input[name*='delivery_time']"
                ).first
                if await delivery_input.count() > 0:
                    try:
                        await delivery_input.fill(delivery)
                    except Exception:
                        await delivery_input.fill(delivery.replace("/", "-").replace(" ", "T"))

            # 建立时间：按 JJ 建立时间填写（如果表单存在该字段）。
            created_input = page.locator(
                "#deposit_order_created_at, "
                "input[name*='created_at']"
            ).first
            if await created_input.count() > 0 and jj.get("created_at"):
                try:
                    await created_input.fill(jj["created_at"])
                except Exception:
                    pass

            submit = await _first_visible(page, [
                "input[type='submit'][value='送出']",
                "input[name='commit']",
                "button[type='submit']",
                "input[type='submit']"
            ], 8000)
            if not submit:
                raise Exception("充值页面找不到「送出」按钮。")

            await submit.click()
            await page.wait_for_load_state("domcontentloaded")

        finally:
            try:
                await browser.close()
            except Exception:
                pass


# 5. 修改商城界面函数（沿用原全部商城后台）
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
                "#admin_user_email, #user_email, input[type='email'], input[name*='email'], input[name*='login'], input[name*='username'], input[type='text']"
            ).first
            await user_input.fill(ADMIN_USER)
            await page.locator("#admin_user_password, #user_password, input[type='password']").first.fill(ADMIN_PASS)
            await page.locator("input[type='submit'], button[type='submit'], input[name='commit']").first.click()
            await page.wait_for_load_state("domcontentloaded")

            await page.goto(f"{BASE_ADMIN_URL}/merchants", wait_until="domcontentloaded")
            search_input = page.locator("input[name*='account'], #search_account, input[type='search'], input[type='text']").first
            await search_input.fill(account_name)
            search_btn = page.locator("button:has-text('搜尋'), button:has-text('搜索'), input[type='submit'], .btn-primary").first
            if await search_btn.is_visible():
                await search_btn.click()
            else:
                await search_input.press("Enter")
            await page.locator("tbody tr").first.wait_for(state="visible", timeout=20000)

            await page.locator("tbody tr").first.locator("a[href$='/edit']").click()
            await page.wait_for_load_state("domcontentloaded")

            shop_template = page.locator("#merchant_store_skin_type")
            if await shop_template.is_visible():
                try:
                    await shop_template.select_option(label=new_skin)
                except Exception:
                    await shop_template.select_option(label=f"预设{new_skin}")

            await page.locator("input[name='commit'][value='送出']").first.click()
            await page.wait_for_load_state("domcontentloaded")
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# 默认主按钮键盘
def build_main_keyboard(account: str, current_skin: str = "极速微商") -> InlineKeyboardMarkup:
    current_skin = current_skin.replace("预设", "")
    buttons = [
        [InlineKeyboardButton(f"✨ 更改商城界面（当前{current_skin}）", callback_data=f"op:{account}:{current_skin}")]
    ]
    return InlineKeyboardMarkup(buttons)


# 展开风格选项键盘
def build_skin_options_keyboard(account: str, current_skin: str = "极速微商") -> InlineKeyboardMarkup:
    current_skin = current_skin.replace("预设", "")
    buttons = [
        [
            InlineKeyboardButton("极速微商", callback_data=f"sk:jisumeishang:{account}"),
            InlineKeyboardButton("七喵", callback_data=f"sk:qimiao:{account}")
        ],
        [
            InlineKeyboardButton("柒月", callback_data=f"sk:qiyue:{account}"),
            InlineKeyboardButton("音你而来", callback_data=f"sk:yinnierlai:{account}")
        ],
        [
            InlineKeyboardButton("⬅️ 收起", callback_data=f"cl:{account}:{current_skin}")
        ]
    ]
    return InlineKeyboardMarkup(buttons)


# 6. Telegram 消息处理
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
        "数字人民币", "數字人民幣", "數位人民幣", "数位人民币", "数字", "數字", "数位", "數位",
        "支付宝", "支付寶", "银行", "銀行", "单笔", "單筆"
    ]
    if not any(k in user_text for k in trigger_keywords):
        return

    parsed_info, error_msg = parse_and_validate_text(user_text)

    if error_msg:
        await update.message.reply_text(error_msg, parse_mode="HTML", disable_web_page_preview=True)
        return

    task_id = f"{update.message.chat_id}_{update.message.message_id}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel:{task_id}")]
    ])

    flow_name = "单笔商城" if parsed_info.get("single_order_no") else "全部商城"
    status_msg = await update.message.reply_text(
        f"⏳ <b>正在进入【{flow_name}】自动建店，请稍候...</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    task = asyncio.create_task(run_shop_worker(status_msg, parsed_info, task_id))

    ACTIVE_TASKS[task_id] = {
        "task": task,
        "page": None,
        "user_id": user_id
    }


# 7. 建店 Worker
async def run_shop_worker(status_msg, parsed_info, task_id: str):
    is_single = bool(parsed_info.get("single_order_no"))

    try:
        if BUILD_SHOP_SEMAPHORE.locked():
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel:{task_id}")]
            ])
            await status_msg.edit_text(
                "⏳ <b>前方有建店任务正在处理中，已自动排队，请稍候...</b>",
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        async with BUILD_SHOP_SEMAPHORE:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel:{task_id}")]
            ])
            await status_msg.edit_text(
                f"⏳ <b>已轮到当前任务，正在【{'单笔商城' if is_single else '全部商城'}】建店...</b>",
                reply_markup=keyboard,
                parse_mode="HTML"
            )

            initial_skin = parsed_info.get("skin", "极速微商").replace("预设", "")

            if is_single:
                result_text, final_account = await _single_create_shop(parsed_info, task_id)

                # 建店完成后查询 JJ。
                order_no = parsed_info["single_order_no"]
                await status_msg.edit_text(
                    result_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ 取消建店", callback_data=f"cancel:{task_id}")]
                    ]),
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )

                jj_result = await _jj_query_order(order_no)

                status_label = "成功" if jj_result.get("status") == "success" else "失败"
                await status_msg.edit_text(
                    f"⏳ <b>单笔商城已建店，JJ 订单状态：{status_label}</b>\n"
                    f"商户：<code>{html.escape(final_account)}</code>\n"
                    f"正在自动填写充值……",
                    parse_mode="HTML"
                )

                await _single_recharge(final_account, jj_result)

                result_text = (
                    "✅ <b>单笔商城全部完成！</b>\n\n"
                    f"店铺账号 : <code>{html.escape(final_account)}</code>\n"
                    "登入密码 : <code>a12345</code>\n"
                    f"JJ状态 : <b>{html.escape(status_label)}</b>\n"
                    "充值资料已自动送出。"
                )
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
            f"❌ 建店出现错误: {safe_err}",
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    finally:
        ACTIVE_TASKS.pop(task_id, None)


# 8. 回调事件处理
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    click_user_id = query.from_user.id

    if data == "ignore":
        await query.answer("⏳ 正在修改界面中，请勿重复点击...", show_alert=False)
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
        _, account, current_skin = data.split(":", 2)
        keyboard = build_skin_options_keyboard(account, current_skin)
        await query.edit_message_reply_markup(reply_markup=keyboard)
        await query.answer()

    elif data.startswith("cl:"):
        _, account, current_skin = data.split(":", 2)
        keyboard = build_main_keyboard(account, current_skin)
        await query.edit_message_reply_markup(reply_markup=keyboard)
        await query.answer()

    elif data.startswith("sk:"):
        _, skin_key, account = data.split(":", 2)
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
            await query.answer(f"✅ 界面已成功更改为: {new_skin_name}", show_alert=True)
        except Exception as e:
            keyboard = build_skin_options_keyboard(account)
            await query.edit_message_reply_markup(reply_markup=keyboard)
            await query.answer(f"❌ 修改界面失败: {str(e)}", show_alert=True)


# 9. 主程序入口
def main():
    if not BOT_TOKEN:
        print("❌ 未检测到 BOT_TOKEN 环境变量！")
        sys.exit(1)

    print("🤖 Telegram 机器人服务运行中...")
    print(f"   全部商城: {'已配置' if BASE_ADMIN_URL else '未配置'}")
    print(f"   单笔商城: {'已配置' if SINGLE_ADMIN_URL else '未配置'}")
    print(f"   JJ后台: {'已配置' if JJ_ADMIN_URL else '未配置'}")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    msg_filter = filters.TEXT & (~filters.COMMAND)

    app.add_handler(MessageHandler(msg_filter, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.run_polling()


if __name__ == "__main__":
    main()
