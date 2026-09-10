import asyncio
import html
import os
import re
from urllib.parse import urlparse
import pyotp
from playwright.async_api import async_playwright
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==================== 环境变量读取 ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0").strip() or 0)

# 普通商城环境变量
ADMIN_URL = os.getenv("ADMIN_URL", "").strip()
ADMIN_USER = os.getenv("ADMIN_USER", "").strip()
ADMIN_PASS = os.getenv("ADMIN_PASS", "").strip()

# 单笔商城环境变量
SINGLE_ADMIN_URL = os.getenv("SINGLE_ADMIN_URL", "").strip()
SINGLE_ADMIN_USER = os.getenv("SINGLE_ADMIN_USER", "").strip()
SINGLE_ADMIN_PASS = os.getenv("SINGLE_ADMIN_PASS", "").strip()

# JJ 后台环境变量
JJ_ADMIN_URL = os.getenv("JJ_ADMIN_URL", "").strip()
JJ_ADMIN_USER = os.getenv("JJ_ADMIN_USER", "").strip()
JJ_ADMIN_PASS = os.getenv("JJ_ADMIN_PASS", "").strip()
JJ_2FA_SECRET = os.getenv("JJ_2FA_SECRET", "").strip()

# 并发控制
QUEUE_SEMAPHORE = asyncio.Semaphore(1)
ACTIVE_TASKS = {}


# ==================== 辅助工具函数 ====================
def _get_clean_domain(url: str) -> str:
  if not url:
    return ""
  url = url.strip()
  # 自动修补：如果没有 http:// 或 https://，自动补齐 https://，防止域名解析混乱
  if not url.startswith("http://") and not url.startswith("https://"):
    url = "https://" + url

  parsed = urlparse(url)
  return f"{parsed.scheme}://{parsed.netloc}"


async def _first_visible(page, selectors, timeout=10000):
  for sel in selectors:
    try:
      loc = page.locator(sel).first
      await loc.wait_for(state="visible", timeout=timeout)
      return loc
    except Exception:
      continue
  return None


# ==================== 消息解析函数 ====================
def parse_message(text: str) -> dict:
  info = {}

  # 提取单笔订单号
  single_match = re.search(
      r"(?:单笔|單筆|订单号|訂單號)\s*[:：]\s*([a-zA-Z0-9\-]+)", text
  )
  if single_match:
    info["single_order_no"] = single_match.group(1).strip()

  # 提取平台账号
  acc_match = re.search(
      r"(?:平台帐号|平台帳號|平台账号|账号|帳號)\s*[:：]\s*([a-zA-Z0-9]+)",
      text,
  )
  if acc_match:
    info["account"] = acc_match.group(1).strip()

  # 提取户名
  name_match = re.search(
      r"(?:户名|戶名|姓名)\s*[:：]\s*([^\n]+)", text
  )
  if name_match:
    info["name"] = name_match.group(1).strip()

  # 提取手机号
  phone_match = re.search(
      r"(?:手机号|手機號|电话|電話)\s*[:：]\s*(\d+)", text
  )
  if phone_match:
    info["phone"] = phone_match.group(1).strip()

  # 提取支付宝/数字人民币/银行卡
  alipay_match = re.search(
      r"(?:支付宝账号|支付寶帳號|支付宝|支付寶)\s*[:：]\s*([^\n]+)",
      text,
  )
  ecny_match = re.search(
      r"(?:数字人民币|數字人民幣)\s*[:：]\s*([^\n]+)", text
  )
  bank_match = re.search(
      r"(?:银行卡|銀行卡|卡号|卡號)\s*[:：]\s*([^\n]+)", text
  )

  if alipay_match:
    info["type"] = "alipay"
    info["alipay_account"] = alipay_match.group(1).strip()
  elif ecny_match:
    info["type"] = "digital_wallet"
    info["digital_account"] = ecny_match.group(1).strip()
  elif bank_match:
    info["type"] = "bank"
    info["bank_account"] = bank_match.group(1).strip()

  # 皮肤/模板
  skin_match = re.search(
      r"(?:模板|界面|皮肤|皮服)\s*[:：]\s*([^\n]+)", text
  )
  info["skin"] = skin_match.group(1).strip() if skin_match else "极速微商"

  return info


# ==================== JJ 查询函数 ====================
async def _query_jj_order(order_no: str, task_id: str) -> dict:
  if not JJ_ADMIN_URL or not JJ_ADMIN_USER or not JJ_ADMIN_PASS:
    raise Exception("未配置 JJ 后台环境变量！")

  domain_root = _get_clean_domain(JJ_ADMIN_URL)

  async with async_playwright() as p:
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox"],
    )
    try:
      page = await browser.new_page()
      page.set_default_timeout(20000)

      # 1. 登录 JJ 后台
      await page.goto(
          f"{domain_root}/admin/login", wait_until="domcontentloaded"
      )

      user_input = page.locator(
          'input[name="username"], input[name="login"], #username'
      ).first
      await user_input.fill(JJ_ADMIN_USER)

      pass_input = page.locator(
          'input[name="password"], #password'
      ).first
      await pass_input.fill(JJ_ADMIN_PASS)

      if JJ_2FA_SECRET:
        totp = pyotp.TOTP(JJ_2FA_SECRET.replace(" ", ""))
        otp_code = totp.now()
        otp_input = page.locator(
            'input[name="otp"], input[name="code"], #otp'
        ).first
        if await otp_input.count() and await otp_input.is_visible():
          await otp_input.fill(otp_code)

      await page.locator(
          'button[type="submit"], input[type="submit"]'
      ).first.click()
      await page.wait_for_load_state("domcontentloaded")

      # 2. 查询订单
      await page.goto(
          f"{domain_root}/admin/shipments", wait_until="domcontentloaded"
      )
      search_input = page.locator(
          'input[name="order_no"], input[type="search"], #search'
      ).first
      await search_input.fill(order_no)
      await search_input.press("Enter")
      await page.wait_for_load_state("domcontentloaded")

      row = page.locator("tbody tr").first
      await row.wait_for(state="visible", timeout=15000)

      status_text = await row.locator("td").nth(2).inner_text()
      tracking_no = await row.locator("td").nth(4).inner_text()
      receiver_info = await row.locator("td").nth(5).inner_text()

      return {
          "status": status_text.strip(),
          "tracking_no": tracking_no.strip(),
          "receiver": receiver_info.strip(),
      }
    finally:
      await browser.close()


# ==================== 单笔商城充值函数 ====================
async def _single_recharge(page, account: str, jj_result: dict) -> str:
  domain_root = _get_clean_domain(SINGLE_ADMIN_URL)

  await page.goto(
      f"{domain_root}/market_managers/recharges/new",
      wait_until="domcontentloaded",
  )

  acc_input = page.locator("#recharge_account, input[name*='account']").first
  await acc_input.fill(account)

  note_input = page.locator(
      "#recharge_remark, textarea[name*='remark']"
  ).first
  remark_content = f"运单号: {jj_result.get('tracking_no', '')} | 收件人: {jj_result.get('receiver', '')}"
  await note_input.fill(remark_content)

  await page.locator('input[type="submit"], button[type="submit"]').first.click()
  await page.wait_for_load_state("domcontentloaded")
  return "充值成功"


# ==================== 单笔商城建店逻辑 ====================
async def _create_single_shop(info: dict, task_id: str):
  domain_root = _get_clean_domain(SINGLE_ADMIN_URL)

  base_account = info["account"]
  target_skin = info.get("skin", "极速微商")
  info_type = info.get("type", "alipay")
  suffix_num = 0
  final_account = base_account

  async with async_playwright() as p:
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox"],
    )
    try:
      context = await browser.new_context()
      page = await context.new_page()
      page.set_default_timeout(20000)
      if task_id in ACTIVE_TASKS:
        ACTIVE_TASKS[task_id]["page"] = page

      # 1. 登录单笔后台
      await page.goto(
          f"{domain_root}/market_managers/sign_in",
          wait_until="domcontentloaded",
      )

      user_input = await _first_visible(
          page,
          [
              'input[name="market_manager[username]"]',
              'input[name="market_manager[login]"]',
              "#market_manager_username",
              'input[type="text"]',
          ],
      )
      await user_input.fill(SINGLE_ADMIN_USER)

      pass_input = await _first_visible(
          page,
          [
              'input[name="market_manager[password]"]',
              "#market_manager_password",
              'input[type="password"]',
          ],
      )
      await pass_input.fill(SINGLE_ADMIN_PASS)

      await page.locator(
          'input[type="submit"], button[type="submit"]'
      ).first.click()
      await page.wait_for_load_state("domcontentloaded")

      # 2. 循环新建商户
      while True:
        current_account = (
            base_account
            if suffix_num == 0
            else f"{base_account}{suffix_num:02d}"
        )

        await page.goto(
            f"{domain_root}/market_managers/merchants/new",
            wait_until="domcontentloaded",
        )

        username_input = await _first_visible(
            page,
            [
                "#merchant_username",
                'input[name="merchant[username]"]',
            ],
        )
        if not username_input:
          raise Exception(
              f"未能加载新建商户页面，当前URL: {page.url}"
          )

        await username_input.fill(current_account)

        for sel in [
            "#merchant_password",
            "#merchant_password_confirmation",
        ]:
          loc = page.locator(sel)
          if await loc.count() and await loc.is_visible():
            await loc.fill("a12345")

        sprite = page.locator("#merchant_sprite_platform")
        if await sprite.count() and await sprite.is_visible():
          try:
            await sprite.select_option(label="jj")
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
            "#merchant_bank_accounts_attributes_0_bank_name"
        ).first
        branch_name_input = page.locator(
            "#merchant_bank_accounts_attributes_0_branch_name"
        ).first
        card_no_input = page.locator(
            "#merchant_bank_accounts_attributes_0_account_no"
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

        alipay_input = page.locator(
            "#merchant_alipay_accounts_attributes_0_account_name"
        ).first
        if await alipay_input.count() and await alipay_input.is_visible():
          await alipay_input.fill(
              info.get("alipay_account", "") if info_type == "alipay" else ""
          )

        shop_template = page.locator("#merchant_store_skin_type").first
        if await shop_template.count() and await shop_template.is_visible():
          try:
            await shop_template.select_option(label=target_skin)
          except Exception:
            pass

        await page.locator('input[name="commit"][value="送出"]').first.click()
        await page.wait_for_load_state("domcontentloaded")

        body_text = await page.locator("body").inner_text()
        if "已经被使用" in body_text or "已經被使用" in body_text:
          suffix_num += 1
          continue

        final_account = current_account
        break

      # 3. 获取店铺链接
      await page.goto(
          f"{domain_root}/market_managers/merchants",
          wait_until="domcontentloaded",
      )
      search_input = await _first_visible(
          page, ["input[name='account']", "#search_account"]
      )
      if search_input:
        await search_input.fill(final_account)
        await search_input.press("Enter")
        await page.locator("tbody tr").first.wait_for(
            state="visible", timeout=15000
        )

      shop_url = ""
      try:
        shop_url = (
            await page.locator("tbody tr")
            .first.locator("td")
            .nth(3)
            .inner_text()
        ).strip()
      except Exception:
        pass

      # 4. 导入商品 60
      await page.locator("tbody tr").first.locator("a[href$='items']").click()
      await page.wait_for_load_state("domcontentloaded")
      import_btn = page.locator('a[href*="items/new"]').first
      await import_btn.click()
      await page.locator("#count_of_items").fill("60")
      await page.locator('input[type="submit"]').click()
      await page.wait_for_load_state("domcontentloaded")

      # 5. JJ 查询 + 充值
      jj_result = await _query_jj_order(info["single_order_no"], task_id)
      recharge_result = await _single_recharge(page, final_account, jj_result)

      msg_text = (
          "✅ <b>单笔商城流程完成！</b>\n\n"
          f"店铺网址： <code>{html.escape(shop_url)}</code>\n"
          f"登入帳號： <code>{html.escape(final_account)}</code>\n"
          "登入密码： <code>a12345</code>\n"
          f"JJ订单状态： <b>{html.escape(jj_result['status'])}</b>\n"
          f"充值结果： <b>{html.escape(recharge_result)}</b>"
      )
      return msg_text, final_account
    finally:
      await browser.close()


# ==================== 普通商城建店逻辑 ====================
async def _create_normal_shop(info: dict, task_id: str):
  domain_root = _get_clean_domain(ADMIN_URL)

  base_account = info["account"]
  target_skin = info.get("skin", "极速微商")
  info_type = info.get("type", "alipay")
  suffix_num = 0
  final_account = base_account

  async with async_playwright() as p:
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox"],
    )
    try:
      context = await browser.new_context()
      page = await context.new_page()
      page.set_default_timeout(20000)
      if task_id in ACTIVE_TASKS:
        ACTIVE_TASKS[task_id]["page"] = page

      # 1. 登录普通后台
      await page.goto(
          f"{domain_root}/admin/login", wait_until="domcontentloaded"
      )

      user_input = await _first_visible(
          page,
          [
              "#merchant_username",
              'input[name="merchant[username]"]',
              'input[name="username"]',
              'input[name="account"]',
          ],
      )
      if not user_input:
        raise Exception(
            f"无法找到登录框！标题：【{await page.title()}】，地址：{page.url}"
        )

      await user_input.fill(ADMIN_USER)

      pass_input = await _first_visible(
          page,
          [
              "#merchant_password",
              'input[name="merchant[password]"]',
              'input[name="password"]',
          ],
      )
      await pass_input.fill(ADMIN_PASS)

      await page.locator(
          'input[type="submit"], button[type="submit"]'
      ).first.click()
      await page.wait_for_load_state("domcontentloaded")

      # 2. 循环建店逻辑
      while True:
        current_account = (
            base_account
            if suffix_num == 0
            else f"{base_account}{suffix_num:02d}"
        )

        await page.goto(
            f"{domain_root}/admin/merchants/new",
            wait_until="domcontentloaded",
        )

        acc_in = await _first_visible(
            page, ["#merchant_username", 'input[name="merchant[username]"]']
        )
        await acc_in.fill(current_account)

        for sel in [
            "#merchant_password",
            "#merchant_password_confirmation",
        ]:
          loc = page.locator(sel)
          if await loc.count() and await loc.is_visible():
            await loc.fill("a12345")

        for sel, val in [
            ("#merchant_account_name", info.get("name", "")),
            ("#merchant_phone", info.get("phone", "")),
        ]:
          loc = page.locator(sel)
          if await loc.count() and await loc.is_visible():
            await loc.fill(val)

        default_num = "6226220809397366"
        bank_name_input = page.locator(
            "#merchant_bank_accounts_attributes_0_bank_name"
        ).first
        branch_name_input = page.locator(
            "#merchant_bank_accounts_attributes_0_branch_name"
        ).first
        card_no_input = page.locator(
            "#merchant_bank_accounts_attributes_0_account_no"
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

        alipay_input = page.locator(
            "#merchant_alipay_accounts_attributes_0_account_name"
        ).first
        if await alipay_input.count() and await alipay_input.is_visible():
          await alipay_input.fill(
              info.get("alipay_account", "") if info_type == "alipay" else ""
          )

        shop_template = page.locator("#merchant_store_skin_type").first
        if await shop_template.count() and await shop_template.is_visible():
          try:
            await shop_template.select_option(label=target_skin)
          except Exception:
            pass

        await page.locator('input[name="commit"][value="送出"]').first.click()
        await page.wait_for_load_state("domcontentloaded")

        body_text = await page.locator("body").inner_text()
        if "已经被使用" in body_text or "已經被使用" in body_text:
          suffix_num += 1
          continue

        final_account = current_account
        break

      # 3. 抓取店铺网址
      await page.goto(
          f"{domain_root}/admin/merchants", wait_until="domcontentloaded"
      )
      search_input = await _first_visible(
          page, ["input[name='account']", "#search_account"]
      )
      if search_input:
        await search_input.fill(final_account)
        await search_input.press("Enter")
        await page.locator("tbody tr").first.wait_for(
            state="visible", timeout=15000
        )

      shop_url = ""
      try:
        shop_url = (
            await page.locator("tbody tr")
            .first.locator("td")
            .nth(3)
            .inner_text()
        ).strip()
      except Exception:
        pass

      # 4. 导入商品 60
      await page.locator("tbody tr").first.locator("a[href$='items']").click()
      await page.wait_for_load_state("domcontentloaded")
      import_btn = page.locator('a[href*="items/new"]').first
      await import_btn.click()
      await page.locator("#count_of_items").fill("60")
      await page.locator('input[type="submit"]').click()
      await page.wait_for_load_state("domcontentloaded")

      msg_text = (
          "✅ <b>普通商城建店完成！</b>\n\n"
          f"店铺网址： <code>{html.escape(shop_url)}</code>\n"
          f"登入帳號： <code>{html.escape(final_account)}</code>\n"
          "登入密码： <code>a12345</code>"
      )
      return msg_text, final_account
    finally:
      await browser.close()


# ==================== Telegram 消息监听 ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
  if not update.message or not update.message.text:
    return

  user_id = update.message.from_user.id
  if ADMIN_USER_ID and user_id != ADMIN_USER_ID:
    return

  text = update.message.text
  info = parse_message(text)

  if "account" not in info:
    return

  task_id = str(update.message.message_id)

  status_msg = await update.message.reply_text("⏳ 正在排队并开始建店，请稍候...")

  async with QUEUE_SEMAPHORE:
    try:
      ACTIVE_TASKS[task_id] = {"status_msg": status_msg}

      # 准确分流逻辑：只有当消息包含单笔订单号时，才走单笔建店，否则一律走普通建店
      if info.get("single_order_no"):
        result_text, acc = await _create_single_shop(info, task_id)
      else:
        result_text, acc = await _create_normal_shop(info, task_id)

      await status_msg.edit_text(result_text, parse_mode="HTML")
    except Exception as e:
      await status_msg.edit_text(f"❌ 建店出现错误: {html.escape(str(e))}")
    finally:
      if task_id in ACTIVE_TASKS:
        del ACTIVE_TASKS[task_id]


# ==================== 程序主入口 ====================
def main():
  if not BOT_TOKEN:
    print("错误: BOT_TOKEN 未设置！")
    return

  app = ApplicationBuilder().token(BOT_TOKEN).build()
  app.add_handler(
      MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
  )

  print("机器人启动中...")
  app.run_polling()


if __name__ == "__main__":
  main()
