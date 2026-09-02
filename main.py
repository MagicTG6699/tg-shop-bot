import re
import html

def parse_and_validate_text(text: str) -> tuple[dict, str]:
    """
    解析并校验提交的文本内容。
    支持自动检测缺少冒号的字段并精准提示错误与提供动态示例。
    """
    info = {}
    errors = []

    # 1. 基础清理（去除 html/url/mailto 标签）
    clean_text = re.sub(r'mailto:', '', text, flags=re.IGNORECASE)
    clean_text = re.sub(r'https?://[^\s]+', '', clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(r'<[^>]+>', '', clean_text)

    # 全局过滤忽略词（包含状态、余额等非必须解析信息）
    base_ignore_keys = ["余额", "餘額", "状态", "狀態", "备注", "備註", "限制", "风控", "風控", "交易日"]
    # 纯文本说明的前缀过滤（防止误报）
    ignore_prefixes = ["注意", "说明", "說明", "提示", "温馨提示", "溫馨提示"]

    lines = clean_text.splitlines()

    # 标准 Key 的正则规则（用于精准捕捉缺少冒号的键名）
    known_keys_pattern = (
        r'(平台帳號|平台账号|会员账号|會員帳號|平台|会员|會員|'
        r'支付宝户名|支付寶戶名|支付宝|支付寶|支付宝账号|支付寶帳號|'
        r'数字人民币|數字人民幣|数币|數幣|钱包|錢包|'
        r'银行名称|銀行名稱|银行|銀行|开户行|開戶行|支行|分行|网点|網點|'
        r'卡号|卡號|账号|帳號|帐号|户名|戶名|姓名|名字|'
        r'手机号|手機號|手机|手機|电话|電話|联系方式)'
    )

    missing_colon_lines = []
    fixed_example_lines = []

    # ----------------------------------------------------
    # 预检阶段：检查是否有包含标准 Key 但未加冒号的情况
    # ----------------------------------------------------
    for line in lines:
        line_str = line.strip()
        # 跳过空行、带忽略词的行或说明前缀行
        if not line_str or any(ik in line_str for ik in base_ignore_keys) or any(line_str.startswith(p) for p in ignore_prefixes):
            continue

        # 如果整行已经包含了中文或英文冒号，跳过冒号预检
        if re.search(r'[:：]', line_str):
            continue

        # 使用正则精准匹配：判断是否符合 “标准Key + 空格/Tab + 内容” 的格式
        match = re.match(f'^{known_keys_pattern}\\s+(.+)$', line_str)
        if match:
            k_name = match.group(1)
            v_val = match.group(2)
            missing_colon_lines.append(f"• 缺少冒号字段：【<b>{html.escape(line_str)}</b>】")
            # 准确生成带冒号的建议修饰示例
            fixed_example_lines.append(f"{html.escape(k_name)}: {html.escape(v_val)}")

    # 如果检测到存在未加冒号的情况，直接打回并输出动态提示
    if missing_colon_lines:
        missing_colon_msg = (
            "❌ <b>格式错误：检测到以下内容缺少冒号！</b>\n\n"
            + "\n".join(missing_colon_lines) +
            "\n\n💡 <b>请参照修改为以下格式后重新发送：</b>\n<code>"
            + "\n".join(fixed_example_lines) +
            "</code>"
        )
        return None, missing_colon_msg

    # ----------------------------------------------------
    # 核心特征与整单类型判定
    # ----------------------------------------------------
    digital_keywords = [
        "数字R人民币", "數字R人民幣", "数字R", "數字R",
        "数字人民币", "數字人民幣", "數位人民幣", "数位人民币",
        "数字名", "數字名", "数位名", "數位名", "数字户名", "數字戶名",
        "数币", "數幣", "数字", "數字", "数位", "數位",
        "钱包", "錢包", "ecny"
    ]
    bank_keywords = ["银行", "銀行", "开户行", "開戶行", "支行"]
    alipay_keywords = ["支付宝", "支付寶", "支付宝户名", "支付寶戶名", "支付宝名", "支付寶名"]

    if any(k in clean_text for k in alipay_keywords):
        info["type"] = "alipay"
    elif any(k in clean_text for k in digital_keywords):
        info["type"] = "digital_wallet"
    elif any(k in clean_text for k in bank_keywords):
        info["type"] = "bank"
    else:
        info["type"] = "alipay"

    raw_accounts = {}
    raw_phone = None
    empty_fields = []

    # ----------------------------------------------------
    # 第一阶段：优先提取【平台会员账号】
    # ----------------------------------------------------
    for line in lines:
        line = line.strip()
        if not line or any(ik in line for ik in base_ignore_keys):
            continue

        if re.search(r'[:：]', line):
            parts = re.split(r'[:：]', line, maxsplit=1)
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

    # ----------------------------------------------------
    # 第二阶段：提取并匹配各项字段
    # ----------------------------------------------------
    for line in lines:
        line = line.strip()
        if not line:
            continue

        has_base_ignore = any(ik in line for ik in base_ignore_keys)
        has_order_and_last = ("订单" in line or "訂單" in line) and ("最后" in line or "最後" in line)
        if has_base_ignore or has_order_and_last:
            continue

        # 仅对带有冒号的行进行正常解析
        if re.search(r'[:：]', line):
            parts = re.split(r'[:：]', line, maxsplit=1)
            key = re.sub(r'\s+', '', parts[0])
            val = parts[1].strip()
            val = re.sub(r'^[<\("‘“]+|[>\)"”]+$', '', val)

            if any(ik in key for ik in base_ignore_keys):
                continue

            if not val:
                if not any(ik in key for ik in ["商城", "模板", "界面"]):
                    empty_fields.append(parts[0].strip())
                continue

            if "account" not in info and key in ["账号", "帳號", "帐号", "会员号", "會員號"]:
                info["account"] = val.lower()

            elif any(k in key for k in [
                "户名", "戶名", "姓名", "名字", "客户姓名", "客戶姓名",
                "支付宝户名", "支付寶戶名", "支付宝名", "支付寶名"
            ]) or key in [
                "名", "数字名", "數字名", "数位名", "數位名",
                "数字人民币户名", "數字人民幣戶名", "數位人民幣戶名", "数位人民币户名"
            ]:
                info["name"] = val

            elif any(k in key for k in ["手机", "手機", "电话", "電話", "联系方式"]):
                raw_phone = val

            elif any(k in key for k in ["商城界面", "商城模板", "界面", "模板"]):
                info["skin"] = val.replace("预设", "")

            elif key in [
                "支付宝", "支付寶", "支付宝账号", "支付寶帳號", "支付宝帐号", "支",
                "支付宝卡号", "支付寶卡號"
            ] or (info.get("type") == "alipay" and info.get("account") and key in ["卡号", "卡號", "账号", "帳號"]):
                raw_accounts["alipay"] = val

            elif key in [
                "数字人民币", "數字人民幣", "數位人民幣", "数位人民币",
                "数字人民币账号", "數字人民幣帳號", "數位人民幣帳號", "数位人民币账号",
                "数字账号", "數字帳號", "数位账号", "數位帳號",
                "数字卡号", "數字卡號", "数位卡号", "數位卡號",
                "数字R人民币", "數字R人民幣", "数字R", "數字R",
                "数币", "數幣", "数字", "數字", "数位", "數位", "钱包", "錢包"
            ] or (info.get("type") == "digital_wallet" and info.get("account") and key in ["账号", "帳號", "卡号", "卡號"]):
                raw_accounts["digital"] = val

            elif any(k in key for k in ["支行", "分行", "网点", "網點", "开户支行", "開戶支行", "银行支行", "銀行支行"]):
                info["branch_name"] = val

            elif any(k in key for k in ["银行名称", "銀行名稱", "开户行", "開戶行", "行名"]) or key in ["银行", "銀行"]:
                if "支行" not in key:
                    if "-" in val or " " in val:
                        bank_parts = re.split(r'[- ]+', val, maxsplit=1)
                        info["bank_name"] = bank_parts[0].strip()
                        info["branch_name"] = bank_parts[1].strip()
                    else:
                        info["bank_name"] = val

            elif key in ["银行账号", "銀行帳號", "银行卡号", "銀行卡號", "银", "銀"] or (
                info.get("type") == "bank" and info.get("account") and key in ["卡号", "卡號", "账号", "帳號"]
            ):
                raw_accounts["bank"] = val

    # ----------------------------------------------------
    # 第三阶段：校验与错误汇总
    # ----------------------------------------------------
    if empty_fields:
        for ef in empty_fields:
            errors.append(f"• 【<b>{html.escape(ef)}</b>】内容为空，请检查是否有漏填！")

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
        if not raw_val and "卡号" not in empty_fields and "账号" not in empty_fields:
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
        if not raw_val and "卡号" not in empty_fields and "账号" not in empty_fields:
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

        if not info.get("bank_name") and "银行" not in empty_fields:
            errors.append("• 缺少【银行名称】！")
        if not info.get("branch_name") and "支行" not in empty_fields:
            errors.append("• 缺少【支行名称】！")

    if errors:
        error_summary = "❌ <b>建店失败！检测到以下输入错误：</b>\n\n" + "\n".join(errors)
        return None, error_summary

    return info, ""


# ==========================================
# 测试运行
# ==========================================
if __name__ == "__main__":
    test_text_no_colon = """平台帳號 test02
支付宝户名 刘斌
卡号 3807536311@qq.com11
手机号 1234567891011
支付宝余额 0
支付宝状态 限制"""

    res_info, res_err = parse_and_validate_text(test_text_no_colon)

    if res_err:
        print(res_err)
    else:
        print("解析成功：", res_info)
