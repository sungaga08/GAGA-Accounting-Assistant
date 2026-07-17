import os
import re
import json
import uuid
import time
import base64
import shutil
import zipfile
import logging
import tempfile
import traceback
from datetime import datetime

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_file,
    send_from_directory,
)
from werkzeug.utils import secure_filename
from openai import OpenAI
from pdf2image import convert_from_path
import requests

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("invoice")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 单文件 10MB 限制

# ============================================================
# API 密钥配置（从环境变量读取）
# ============================================================
AGNES_API_KEY = os.environ.get("AGNES_API_KEY")

# 智谱 GLM-4V-Flash 备用 API 密钥（可选）
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY")

# ============================================================
# 飞书多维表格配置（从环境变量读取）
# ============================================================
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET")

# ============================================================
# 环境变量校验
# ============================================================
REQUIRED_ENV_VARS = [
    "AGNES_API_KEY",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
]

missing_vars = [var for var in REQUIRED_ENV_VARS if not os.environ.get(var)]
if missing_vars:
    raise RuntimeError(f"缺少必需的环境变量: {', '.join(missing_vars)}。请在运行前设置这些环境变量。")
# 多表格配置：id 为前端标识，name 为显示名称，app_token/table_id 为飞书表格标识
# field_map：飞书列名 -> 后端 summary 字段名
FEISHU_APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN")
FEISHU_DINING_TABLE_ID = os.environ.get("FEISHU_DINING_TABLE_ID")
FEISHU_OTHER_TABLE_ID = os.environ.get("FEISHU_OTHER_TABLE_ID")

FEISHU_TABLES = [
    {
        "id": "dining",
        "name": "餐饮导入",
        "app_token": FEISHU_APP_TOKEN,
        "table_id": FEISHU_DINING_TABLE_ID,
        "field_map": {"日期": "date", "类型": "meal_type", "金额": "total_amount"},
    },
    {
        "id": "other",
        "name": "其他导入",
        "app_token": FEISHU_APP_TOKEN,
        "table_id": FEISHU_OTHER_TABLE_ID,
        "field_map": {"日期": "date", "类型": "meal_type", "内容": "content", "人员": "person", "金额": "total_amount"},
    },
]

ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}

AGNES_BASE_URL = "https://apihub.agnes-ai.com/v1"
AGNES_MODEL = "agnes-2.0-flash"
AGNES_TIMEOUT = 30
AGNES_MAX_RETRIES = 2
AGNES_RETRY_DELAY = 2

ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_MODEL = "glm-4v-flash"
ZHIPU_TIMEOUT = 30
ZHIPU_MAX_RETRIES = 1

PROMPT_TEMPLATE = """请分析这张图片/PDF文件，判断它属于以下七种类型中的哪一种：

1. 发票：正规税务发票（电子发票或纸质发票）。特征：通常为A4纸大小或卷式发票，纯白背景，有发票代码、发票号码、纳税人识别号、开票日期、校验码等。注意：发票上通常没有"单价×数量"的商品清单，即使有也更规范。但是，如果发票中包含"配送服务费"、"配送费"、"外卖配送费"等关键词，则归类为"配送费"类型（见下方配送费专属规则）。
2. 小票：超市/餐厅/商店打印的购物小票或水单。特征：纸张较薄，多为热敏纸（手感光滑），通常为手机随手拍摄，背景杂乱（桌面、手、其他物品等），内容通常包括商品清单、单价、数量、合计金额、支付方式（现金/微信/支付宝）等。关键词：商品名称、单价、数量、小计、合计、实付、找零。
   ⚠️ 小票底部常见的开发票提示文字（如"开发票"、"扫码开发票"、"扫一扫开发票"、"如需发票"、"请扫码开票"、"发票请关注公众号"），这些是商家提示如何索要发票的说明，不代表这张纸本身是发票。
3. 支付截图：手机支付成功的截图，通常包含"支付成功"、"微信支付"、"支付宝"等字样，以及支付金额、支付时间、交易单号等。
4. 外卖实物照片：拍摄的菜品/食物/外卖的照片，画面中有餐盒、盘子、食物等，没有文字或文字很少。
5. 打车：行程单或打车发票。包含"客运服务费"、"旅客运输服务"、"运输服务"、"行程单"、"行程信息"、"起点"、"终点"、"出租车"、"滴滴"、"出行"等关键词。需要进一步判断子类型，见下方打车类型专属规则。
6. 高铁：铁路电子客票或火车票，包含"铁路电子客票"、"二等座"、"一等座"、"商务座"、"动车组"、"火车票"等关键词。

请严格按照以下规则判断：
- 如果图片中有食物、菜品、外卖盒、餐盘等实物，且没有明显的发票/小票/支付截图特征，则归类为"外卖实物照片"。
- 如果图片中有人手持菜品或餐桌上的食物照片，也归类为"外卖实物照片"。
- 如果出现"客运服务费"、"旅客运输服务"、"运输服务"、"行程单"、"行程信息"、"出租车"、"滴滴"、"出行"等关键词，优先归类为"打车"。
- 如果出现"配送服务费"、"配送费"、"外卖配送费"等关键词，且文件为发票格式，优先归类为"配送费"。
- 如果出现"铁路电子客票"、"二等座"、"一等座"、"商务座"、"动车组"、"火车票"等关键词，优先归类为"高铁"。
- 小票与发票区分规则（按以下优先级依次判断，一旦命中即停止）：

  第一优先级：关键字强制判定
  以下关键字出现时，强制归类为"小票"：
  "开发票"、"扫码开发票"、"扫一扫开发票"、"如需发票"、"请扫码开票"、"发票请关注公众号"等类似提示文字
  ⚠️ 这些通常印在小票底部，是商家提示顾客如何索要发票的说明，不代表这张纸是发票

  第二优先级：图片背景特征（仅当第一优先级未触发时）
  └─ 纯白底、A4纸质感、平整无折痕 → 归类为"发票"
  └─ 背景杂乱（桌面、手、其他物品、彩色背景）→ 归类为"小票"

  第三优先级：其他辅助特征（仅当前两级都无法判断时）
  └─ 有"统一社会信用代码"或"纳税人识别号" → 归类为"发票"
  └─ 有"电子发票（普通发票）"字样 → 归类为"发票"
  └─ 有商品清单（单价×数量格式）→ 归类为"小票"
  └─ 无法判断 → 默认归类为"小票"

然后提取以下信息：
- 日期（格式：X月X日，如果年份是2026年则省略年份）
- 消费类型（根据支付时间或内容判断：10:00-15:00为午餐，15:00-21:00为晚餐；打车/高铁类型消费类型填对应类型名）
- 金额（数字，保留两位小数）
- 如果是打车/高铁类型，额外提取内容字段（行程/站点信息）
- 如果是支付截图，额外提取支付时间（必须从截图中直接读取，格式如"18:30"、"18时30分"、"18:30:00"）
- 如果是打车类型，额外提取"文件类型"字段（"发票"或"行程单"），区分规则见下方打车类型专属规则

配送费类型专属规则（仅当识别为配送费时生效）：
配送费只有一种文件类型：发票。
- 日期：从发票的"开票日期"提取，格式为"X月X日"
- 金额：从发票的"金额"提取
- 消费类型：固定为"配送费"

打车类型专属规则（仅当识别为打车时生效）：
打车类型分为两个子类型：

1. 打车发票（文件类型标识："发票"）
   特征关键词："客运服务费"、"旅客运输服务"、"运输服务"、"发票"字样、"纳税人识别号"
   通常为 A4 纸大小的电子发票或纸质发票
   示例：标题含"客运服务费电子发票"、有发票号码/开票日期/金额

2. 行程单（文件类型标识："行程单"）
   特征关键词："行程单"、"行程信息"、"起点"、"终点"、"行程起止日期"、"出发地"、"目的地"
   通常显示具体行程路线
   示例：标题含"行程单"字样、有"起点-终点"信息、有"行程起止日期"字段

判断优先级：
- 如果文件同时包含"发票"和"行程信息"，优先判断为"发票"
- 如果文件只有行程信息（起点-终点、行程日期等），没有发票字样 → 判断为"行程单"
- 如果文件名或内容明确包含"行程单"字样 → 判断为"行程单"

打车类型字段提取规则：
- 日期：统一从"行程起止日期"中提取（打车发票和行程单都使用同一来源），格式为"X月X日"（如2026年07月17日→7月17日）
- 金额：从"金额"或"票价"字段中提取，保留两位小数。优先使用"金额"
- 内容：从"起点-终点"或"出发地-目的地"中提取，格式为"XX站-XX站"或"XX地-XX地"
- 文件类型：根据子类型判断结果填写"发票"或"行程单"，无法区分默认填"行程单"

高铁电子客票识别补充规则（仅当识别为高铁电子客票时生效，优先级高于默认规则）：
1. 所有字段必须直接摘录图片中的原始中文文字，不进行翻译、纠错、补全或猜测。
2. 如果图片同时存在中文、英文、拼音，优先提取中文，忽略英文和拼音。例如图片中有"杭州东站"和"Hangzhoudong"，应输出"杭州东站"，绝不能输出英文或拼音。

【站点提取规则】
从图片中查找中文站名，格式通常为"xx站→xx站"或"xx站-xx站"。直接提取中文站名原文，保留"站"字，不要翻译、不要修正、不要猜测。如果OCR识别出的文字可能有误，请按原文输出，不要自行"纠正"。
例如：图片中有"杭州东站→长沙南站"，应输出"杭州东站-长沙南站"

【姓名提取流程】
第一步：找到身份证号码（通常为xxxxxxxxxxxx****1234格式）。
第二步：读取身份证号码附近所有中文文字。
第三步：寻找距离身份证号码最近的2~5个连续中文汉字。
第四步：该字段即为乘车人姓名。
第五步：如果找到多个姓名，只取距离身份证号码最近的一个。
第六步：如果无法确认姓名，请输出空字符串""。
禁止输出：未知、Unknown、姓名未知或其他模糊占位符。

【日期提取规则】
从"开票日期"或"乘车日期"中提取，格式为"X月X日"。例如：2026年06月30日→6月30日

【金额提取规则】
提取票价数字，保留两位小数。例如：￥485.00→485.00

请只返回JSON格式结果，不要包含其他文字：
- 普通类型（发票/小票/外卖实物照片）：{"类型": "发票", "日期": "X月X日", "消费类型": "午餐/晚餐", "金额": 128.00}
- 支付截图：{"类型": "支付截图", "日期": "X月X日", "消费类型": "晚餐", "金额": 128.00, "支付时间": "18:30"}
- 打车类型：{"类型": "打车", "日期": "7月17日", "消费类型": "打车", "金额": 68.50, "内容": "北京站-首都机场", "文件类型": "行程单"}
- 高铁类型：{"类型": "高铁", "日期": "6月30日", "消费类型": "高铁", "金额": 485.00, "内容": "杭州东站-长沙南站", "人名": "李云龙"}
- 配送费类型：{"类型": "配送费", "日期": "X月X日", "消费类型": "配送费", "金额": 6.50}

如果无法识别或图片不清晰，请返回：{"类型": "未知", "日期": "", "消费类型": "", "金额": 0}"""


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_file_extension(filename):
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


def file_to_data_uri(file_path):
    with open(file_path, "rb") as f:
        file_data = f.read()
    ext = get_file_extension(file_path)
    mime_map = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
    }
    media_type = mime_map.get(ext, "application/octet-stream")
    return f"data:{media_type};base64,{base64.b64encode(file_data).decode('utf-8')}"


def convert_pdf_to_image(pdf_path, temp_dir):
    """将 PDF 第一页转换为 JPG 图片，返回图片路径，失败返回 None"""
    try:
        images = convert_from_path(pdf_path, first_page=1, last_page=1, dpi=200)
        if not images:
            logger.warning(f"PDF 转换失败：未生成任何图片: {pdf_path}")
            return None
        img_path = os.path.join(temp_dir, "pdf_converted.jpg")
        images[0].save(img_path, "JPEG", quality=85)
        logger.info(f"PDF 第一页已转换为图片: {img_path}")
        return img_path
    except Exception as e:
        logger.error(f"PDF 转换异常: {pdf_path}: {e}")
        return None


def parse_ai_response(raw_content):
    """
    从 AI 返回的原始文本中提取 JSON。
    支持多种格式：纯 JSON、被 ```json 包裹、包含额外文字等。
    如果解析结果为列表，取第一个有效元素。
    """
    content = raw_content.strip()
    logger.debug(f"parse_ai_response 原始内容 (前500字符): {content[:500]}")

    def _ensure_dict(obj):
        """如果 obj 是列表，取第一个有效字典元素；否则直接返回"""
        if isinstance(obj, list):
            if len(obj) > 0 and isinstance(obj[0], dict):
                logger.info(f"AI 返回了列表（长度={len(obj)}），取第一个元素")
                return obj[0]
            logger.warning(f"AI 返回了列表但第一个元素不是字典，返回 None")
            return None
        return obj

    # 尝试 1：直接解析
    try:
        result = json.loads(content)
        result = _ensure_dict(result)
        if result:
            logger.debug(f"直接 JSON 解析成功: {result}")
            return result
    except json.JSONDecodeError:
        pass

    # 尝试 2：提取 ```json ... ``` 或 ``` ... ``` 中的内容
    fenced_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
    if fenced_match:
        try:
            result = json.loads(fenced_match.group(1).strip())
            result = _ensure_dict(result)
            if result:
                logger.debug(f"从代码块中提取 JSON 成功: {result}")
                return result
        except json.JSONDecodeError:
            pass

    # 尝试 3：查找第一个 { 到最后一个 } 之间的内容
    brace_match = re.search(r"\{.*\}", content, re.DOTALL)
    if brace_match:
        try:
            result = json.loads(brace_match.group(0))
            result = _ensure_dict(result)
            if result:
                logger.debug(f"从大括号中提取 JSON 成功: {result}")
                return result
        except json.JSONDecodeError:
            pass

    # 尝试 4：替换中文引号为英文引号后再解析
    try:
        fixed = content.replace("\u201c", '"').replace("\u201d", '"')
        fixed = fixed.replace("\u2018", "'").replace("\u2019", "'")
        fixed = fixed.replace("\uff1a", ":")
        fixed = re.sub(r"(\d+)\.(\d+)(?!\d)", r"\1.\2", fixed)
        brace_match2 = re.search(r"\{.*\}", fixed, re.DOTALL)
        if brace_match2:
            result = json.loads(brace_match2.group(0))
            result = _ensure_dict(result)
            if result:
                logger.debug(f"替换引号后提取 JSON 成功: {result}")
                return result
    except (json.JSONDecodeError, Exception):
        pass

    logger.warning(f"无法解析 AI 返回内容: {content[:300]}")
    return None


def validate_result(result):
    """验证解析结果是否有效"""
    if result is None or not isinstance(result, dict):
        return False
    valid_types = {"发票", "小票", "支付截图", "外卖实物照片", "打车", "高铁", "配送费", "未知"}
    file_type = result.get("类型", "")
    if file_type not in valid_types:
        return False
    if file_type == "未知":
        return True
    date_val = result.get("日期", "")
    amount_val = result.get("金额")
    if not date_val:
        return False
    if amount_val is None or (isinstance(amount_val, (int, float)) and amount_val == 0):
        return False
    return True


def call_vision_api(base_url, api_key, model, timeout, max_retries, retry_delay, file_path, provider_name):
    """
    通用视觉 API 调用，支持 Agnes AI 和智谱 GLM-4V-Flash。
    文本和图片合并到同一条消息的 content 数组中。
    """
    data_uri = file_to_data_uri(file_path)
    logger.info(f"[{provider_name}] 开始调用 API，文件: {file_path}，模型: {model}")

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            logger.debug(f"[{provider_name}] 第 {attempt + 1}/{max_retries + 1} 次尝试")

            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": PROMPT_TEMPLATE},
                            {"type": "image_url", "image_url": {"url": data_uri}},
                        ],
                    }
                ],
                timeout=timeout,
            )

            raw_content = response.choices[0].message.content
            logger.info(f"[{provider_name}] API 原始返回 (前500字符): {raw_content[:500]}")

            result = parse_ai_response(raw_content)
            if result is None:
                logger.warning(f"[{provider_name}] JSON 解析失败，原始返回: {raw_content[:300]}")
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                return {"类型": "未知", "日期": "", "消费类型": "", "金额": 0}

            if not isinstance(result, dict):
                logger.warning(f"[{provider_name}] AI 返回结果不是字典，类型为 {type(result).__name__}，内容: {str(result)[:200]}")
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                return {"类型": "未知", "日期": "", "消费类型": "", "金额": 0}

            if not validate_result(result):
                logger.warning(f"[{provider_name}] 结果校验不通过: {result}")
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                return {"类型": "未知", "日期": "", "消费类型": "", "金额": 0}

            logger.info(f"[{provider_name}] 识别成功: {result}")
            return result

        except Exception as e:
            last_error = e
            logger.error(f"[{provider_name}] API 调用异常 (第{attempt + 1}次): {type(e).__name__}: {e}")
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue

    logger.error(f"[{provider_name}] 所有重试均失败，最后错误: {last_error}")
    raise last_error if last_error else Exception(f"[{provider_name}] 所有重试均失败")


def call_zhipu_api(file_path, temp_dir):
    """直接调用智谱 GLM-4V-Flash 视觉识别 API"""
    actual_path = file_path
    if get_file_extension(file_path) == "pdf":
        converted = convert_pdf_to_image(file_path, temp_dir)
        if converted is None:
            raise RuntimeError(f"PDF转换失败，无法识别: {os.path.basename(file_path)}")
        actual_path = converted

    return call_vision_api(
        base_url=ZHIPU_BASE_URL,
        api_key=ZHIPU_API_KEY,
        model=ZHIPU_MODEL,
        timeout=ZHIPU_TIMEOUT,
        max_retries=ZHIPU_MAX_RETRIES,
        retry_delay=2,
        file_path=actual_path,
        provider_name="Zhipu",
    )


def time_str_to_meal_type(time_str):
    """根据支付时间字符串判断午餐/晚餐，支持 18:30 / 18时30分 / 18:30:00 等格式"""
    if not time_str:
        return ""
    match = re.search(r"(\d{1,2})\s*[:：时]\s*(\d{1,2})", time_str)
    if not match:
        return ""
    hour = int(match.group(1))
    if 10 <= hour < 15:
        return "午餐"
    elif 15 <= hour < 21:
        return "晚餐"
    return ""

def determine_baseline(results, manual_meal_type=None):
    """
    多源混合优先级确定基准信息。
    
    各字段独立按优先级选取来源：
    - 金额：发票 > 支付截图 > 小票 > 第一个成功识别的文件
    - 日期：支付截图 > 小票 > 发票 > 第一个成功识别的文件
    - 消费类型：用户手动输入 > AI识别出"打车"/"高铁"/"配送费" > 支付截图时间判断 > 小票AI判断 > 发票AI判断 > 默认"午餐"
    - 内容（trip_route）：从打车/高铁类型文件中提取"内容"字段
    """
    payment_screenshots = []
    invoices = []
    receipts = []
    taxi_files = []
    train_files = []
    delivery_files = []
    first_success = None

    for r in results:
        if r is None:
            continue
        t = r.get("type", "")
        if t == "支付截图":
            payment_screenshots.append(r)
            if first_success is None:
                first_success = r
        elif t == "发票":
            invoices.append(r)
            if first_success is None:
                first_success = r
        elif t == "小票":
            receipts.append(r)
            if first_success is None:
                first_success = r
        elif t == "打车":
            taxi_files.append(r)
            if first_success is None:
                first_success = r
        elif t == "高铁":
            train_files.append(r)
            if first_success is None:
                first_success = r
        elif t == "配送费":
            delivery_files.append(r)
            if first_success is None:
                first_success = r
        elif first_success is None:
            first_success = r

    def get_first_from_order(order_list):
        """从优先级列表中返回第一个有效来源及其标签，都没有则返回 first_success"""
        for source_list, label in order_list:
            if source_list:
                return source_list[0], label
        return first_success, "第一个成功识别的文件"

    # 1. 金额：发票 > 支付截图 > 小票 > 打车 > 高铁 > 第一个成功
    total_amount = 0
    amount_source, amount_label = get_first_from_order([
        (invoices, "发票"),
        (payment_screenshots, "支付截图"),
        (receipts, "小票"),
        (taxi_files, "打车"),
        (train_files, "高铁"),
    ])
    if amount_source:
        total_amount = abs(float(amount_source.get("amount", 0) or 0))
        logger.info(f"金额来源: {amount_label}, 值={total_amount}")
    else:
        logger.warning("无法确定基准金额，使用0")

    # 2. 日期：支付截图 > 小票 > 发票 > 打车 > 高铁 > 第一个成功
    base_date = ""
    date_source, date_label = get_first_from_order([
        (payment_screenshots, "支付截图"),
        (receipts, "小票"),
        (invoices, "发票"),
        (taxi_files, "打车"),
        (train_files, "高铁"),
    ])
    if date_source:
        base_date = date_source.get("date", "")
        logger.info(f"日期来源: {date_label}, 值={base_date}")
    else:
        logger.warning("无法确定基准日期，使用空字符串")

    # 3. 消费类型：手动输入 > AI识别"打车"/"高铁"/"配送费" > 支付截图时间判断 > 小票AI判断 > 发票AI判断 > 默认"午餐"
    if manual_meal_type and manual_meal_type.strip():
        base_meal_type = manual_meal_type.strip()
        logger.info(f"消费类型来源: 用户手动输入, 值={base_meal_type}")
    elif train_files:
        base_meal_type = "高铁"
        logger.info(f"消费类型来源: AI识别为高铁, 值={base_meal_type}")
    elif taxi_files:
        base_meal_type = "打车"
        logger.info(f"消费类型来源: AI识别为打车, 值={base_meal_type}")
    elif delivery_files:
        base_meal_type = "配送费"
        logger.info(f"消费类型来源: AI识别为配送费, 值={base_meal_type}")
    elif payment_screenshots:
        ps = payment_screenshots[0]
        pay_time = ps.get("payment_time", "") or ps.get("支付时间", "")
        time_meal = time_str_to_meal_type(pay_time)
        if time_meal:
            base_meal_type = time_meal
            logger.info(f"消费类型来源: 支付截图时间判断 (支付时间={pay_time}), 值={base_meal_type}")
        else:
            base_meal_type = ps.get("meal_type", "") or "午餐"
            logger.info(f"消费类型来源: 支付截图AI判断 (无有效支付时间，支付时间原始值={pay_time}), 值={base_meal_type}")
    else:
        meal_source, meal_label = get_first_from_order([
            (receipts, "小票"),
            (invoices, "发票"),
        ])
        if meal_source:
            base_meal_type = meal_source.get("meal_type", "") or "午餐"
            logger.info(f"消费类型来源: {meal_label}的AI判断, 值={base_meal_type}")
        else:
            base_meal_type = "午餐"
            logger.info("消费类型来源: 默认, 值=午餐")

    # 4. 内容（trip_route）：从打车或高铁文件中提取，确保为纯字符串
    trip_route = ""
    for src_files in [train_files, taxi_files]:
        if src_files:
            raw_content = src_files[0].get("content", "") or ""
            if isinstance(raw_content, list):
                trip_route = ", ".join(str(item) for item in raw_content)
            else:
                trip_route = str(raw_content)
            if trip_route:
                logger.info(f"行程内容: {trip_route}")
                break

    logger.info(f"最终基准信息: 日期={base_date}, 消费类型={base_meal_type}, 总金额={total_amount}, 内容={trip_route}")
    return base_date, base_meal_type, total_amount, [], trip_route


def generate_new_filename(result, base_date, base_meal_type, total_amount, index_in_type):
    """根据识别结果和基准信息生成新文件名。高铁/打车类型使用自身的日期/金额。"""
    file_type = result.get("type", "")
    ext = result.get("_ext", "jpg")

    # 高铁类型：使用自身的日期、人名、金额，格式 X月X日-人名-高铁-XX元.扩展名
    if file_type == "高铁":
        date_str = result.get("date", "")
        person = result.get("person", "") or "未知"
        amount = float(result.get("amount", 0) or 0)
        if amount == int(amount):
            amount_str = f"{int(amount)}"
        else:
            amount_str = f"{amount:.2f}".rstrip("0").rstrip(".")
        seq = str(index_in_type) if (index_in_type is not None and index_in_type > 0) else ""
        name = f"{date_str}-{person}-高铁-{amount_str}元{seq}.{ext}"
        logger.debug(f"生成高铁文件名: {name} (序号={index_in_type})")
        return name

    # 打车类型：使用自身的日期、金额，根据文件类型选择后缀
    if file_type == "打车":
        date_str = result.get("date", "") or base_date
        amount = float(result.get("amount", 0) or 0)
        if amount == int(amount):
            amount_str = f"{int(amount)}"
        else:
            amount_str = f"{amount:.2f}".rstrip("0").rstrip(".")
        doc_type = result.get("doc_type", "") or result.get("文件类型", "行程单")
        suffix = "行程单" if doc_type == "行程单" else "发票"
        seq = str(index_in_type) if (index_in_type is not None and index_in_type > 0) else ""
        name = f"{date_str}-打车-{amount_str}元-{suffix}{seq}.{ext}"
        logger.debug(f"生成打车文件名: {name} (自身日期={result.get('date')}, 自身金额={result.get('amount')}, 文件类型={doc_type})")
        return name

    # 配送费类型：使用自身的日期、金额，格式 X月X日-配送费-X元.扩展名
    if file_type == "配送费":
        date_str = result.get("date", "") or base_date
        amount = float(result.get("amount", 0) or 0)
        if amount == int(amount):
            amount_str = f"{int(amount)}"
        else:
            amount_str = f"{amount:.2f}".rstrip("0").rstrip(".")
        seq = str(index_in_type) if (index_in_type is not None and index_in_type > 0) else ""
        name = f"{date_str}-配送费-{amount_str}元{seq}.{ext}"
        logger.debug(f"生成配送费文件名: {name} (自身日期={result.get('date')}, 自身金额={result.get('amount')})")
        return name

    date_str = base_date if base_date else result.get("date", "")
    meal_str = base_meal_type if base_meal_type else result.get("meal_type", "")

    if isinstance(total_amount, float) and total_amount == int(total_amount):
        amount_str = f"{int(total_amount)}"
    else:
        amount_str = f"{total_amount:.2f}".rstrip("0").rstrip(".")

    seq = str(index_in_type) if (index_in_type is not None and index_in_type > 0) else ""

    type_templates = {
        "支付截图": f"{date_str}-{meal_str}-{amount_str}元-支付截图{seq}.{ext}",
        "发票": f"{date_str}-{meal_str}-{amount_str}元-发票{seq}.{ext}",
        "小票": f"{date_str}-{meal_str}-{amount_str}元-小票{seq}.{ext}",
        "外卖实物照片": f"{date_str}-{meal_str}-{amount_str}元{seq}.{ext}",
    }

    name = type_templates.get(file_type, f"{date_str}-{meal_str}-{amount_str}元{seq}.{ext}")
    logger.debug(f"生成文件名: {name} (类型={file_type}, 序号={index_in_type})")
    return name


# ============================================================
# 飞书多维表格 API
# ============================================================

def _get_feishu_tenant_token():
    """获取飞书 tenant_access_token"""
    logger.info("[飞书] 获取 tenant_access_token")
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=15,
    )
    logger.debug(f"[飞书] token 响应: HTTP {resp.status_code}, body={resp.text[:300]}")
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书 token 失败: {data.get('msg', '未知错误')} (code={data.get('code')})")
    token = data["tenant_access_token"]
    logger.info("[飞书] tenant_access_token 获取成功")
    return token


def write_to_feishu_bitable(table_config, summaries):
    """根据表格配置将汇总信息写入飞书多维表格。summaries 可为单个 dict 或 dict 列表。"""
    if isinstance(summaries, dict):
        summaries = [summaries]

    token = _get_feishu_tenant_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{table_config['app_token']}/tables/{table_config['table_id']}/records/batch_create"

    field_map = table_config["field_map"]
    records = []
    for summary in summaries:
        fields = {}
        for feishu_col, summary_key in field_map.items():
            val = summary.get(summary_key)
            if val is not None and val != "":
                if isinstance(val, list):
                    val = ", ".join(str(v) for v in val)
                elif not isinstance(val, (str, int, float)):
                    val = str(val)
                if val != "":
                    fields[feishu_col] = val
        records.append({"fields": fields})

    payload = {"records": records}

    logger.info(f"[飞书] 写入表格 {table_config['name']} ({len(records)}条记录): {json.dumps(records, ensure_ascii=False)[:500]}")
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    logger.debug(f"[飞书] 写入响应: HTTP {resp.status_code}, body={resp.text[:500]}")
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"写入飞书表格失败: {data.get('msg', '未知错误')} (code={data.get('code')})")
    logger.info(f"[飞书] 写入成功 ({len(records)}条)")
    return data


# ============================================================
# Flask 路由
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload_and_process():
    try:
        files = request.files.getlist("files")
        manual_meal_type = request.form.get("meal_type", "").strip()
        logger.info(f"收到上传请求，文件数: {len(files)}, 手动消费类型: {manual_meal_type or '无'}")

        if not files or all(f.filename == "" for f in files):
            return jsonify({"success": False, "error": "请先选择文件"})

        if len(files) > 10:
            return jsonify({"success": False, "error": "一次最多上传10个文件"})

        if ZHIPU_API_KEY == "请替换成你的智谱API密钥":
            return jsonify({"success": False, "error": "请先在 main.py 中配置 ZHIPU_API_KEY"})

        session_id = str(uuid.uuid4())
        temp_dir = os.path.join(tempfile.gettempdir(), f"invoice_{session_id}")
        os.makedirs(temp_dir, exist_ok=True)
        logger.info(f"会话: {session_id}, 临时目录: {temp_dir}")

        # 验证并保存文件
        saved_files = []
        validation_errors = []
        for i, f in enumerate(files):
            if f.filename == "":
                continue
            if not allowed_file(f.filename):
                err = f"不支持该文件格式 (.{get_file_extension(f.filename)})，请上传PDF或图片: {f.filename}"
                validation_errors.append(err)
                logger.warning(err)
                continue
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(0)
            if size > 10 * 1024 * 1024:
                err = f"文件过大，请压缩后重试: {f.filename} ({size / 1024 / 1024:.1f}MB)"
                validation_errors.append(err)
                logger.warning(err)
                continue

            filename = secure_filename(f.filename)
            save_path = os.path.join(temp_dir, f"original_{i}_{filename}")
            f.save(save_path)
            saved_files.append(
                {
                    "index": i,
                    "original_name": f.filename,
                    "saved_path": save_path,
                    "ext": get_file_extension(f.filename),
                }
            )
            logger.info(f"文件已保存: {f.filename} -> {save_path} ({size} bytes)")

        if validation_errors:
            if not saved_files:
                shutil.rmtree(temp_dir, ignore_errors=True)
                return jsonify({"success": False, "error": "; ".join(validation_errors)})

        if not saved_files:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify({"success": False, "error": "请先选择文件"})

        logger.info(f"有效文件数: {len(saved_files)}, 验证错误数: {len(validation_errors)}")

        # 逐个文件调用 AI 识别
        recognition_results = []
        api_errors = []
        processed_files = []

        for sf in saved_files:
            logger.info(f"处理文件 [{sf['index']}]: {sf['original_name']}")
            try:
                result = call_zhipu_api(sf["saved_path"], temp_dir)
                result["_ext"] = sf["ext"]
                result["_original_name"] = sf["original_name"]
                result["_saved_path"] = sf["saved_path"]
                result["_index"] = sf["index"]

                if result.get("类型") in ("未知", "", None):
                    result["类型"] = "外卖实物照片"
                    logger.info(f"兜底归类: {sf['original_name']} -> 外卖实物照片 (日期={result.get('日期')}, 金额={result.get('金额')})")

                pf = {
                    "type": result.get("类型"),
                    "date": result.get("日期"),
                    "meal_type": result.get("消费类型"),
                    "amount": float(result.get("金额", 0) or 0),
                    "content": result.get("内容", ""),  # 打车行程/高铁站点
                    "person": result.get("人名", ""),   # 高铁乘车人姓名
                    "payment_time": result.get("支付时间", ""),  # 支付截图时间
                    "doc_type": result.get("文件类型", "行程单"),  # 打车子类型
                    "_ext": sf["ext"],
                    "_original_name": sf["original_name"],
                    "_saved_path": sf["saved_path"],
                    "_index": sf["index"],
                    "_skipped": False,
                }
                processed_files.append(pf)
                recognition_results.append(pf)
                logger.info(f"识别成功: {sf['original_name']} -> 类型={pf['type']}, 日期={pf['date']}, 金额={pf['amount']}")

                # 高铁类型后处理校验
                if pf.get("type") == "高铁":
                    person = pf.get("person", "")
                    if person in ("", "未知", "？", "*"):
                        logger.warning(f"高铁票人名提取失败（'{person}'），请手动核对: {sf['original_name']}")
                    content = pf.get("content", "")
                    suspicious_chars = ["咚", "哪", "那", "呐"]
                    for ch in suspicious_chars:
                        if ch in content:
                            logger.warning(f"高铁票站点名称可能OCR有误（含'{ch}'）: {content}，文件: {sf['original_name']}")
                            break

            except Exception as e:
                error_msg = f"AI识别失败：{e}，请检查网络或API密钥: {sf['original_name']}"
                logger.error(f"识别异常 [{sf['original_name']}]: {e}\n{traceback.format_exc()}")
                recognition_results.append(
                    {
                        "type": "未知",
                        "date": "",
                        "meal_type": "",
                        "amount": 0,
                        "payment_time": "",
                        "content": "",
                        "person": "",
                        "doc_type": "",
                        "_ext": sf["ext"],
                        "_original_name": sf["original_name"],
                        "_saved_path": sf["saved_path"],
                        "_index": sf["index"],
                        "_skipped": True,
                    }
                )
                api_errors.append(error_msg)

        if not processed_files:
            shutil.rmtree(temp_dir, ignore_errors=True)
            error_detail = "; ".join(api_errors) if api_errors else "所有文件均识别失败，请检查图片清晰度后重试"
            logger.error(f"无有效文件，返回错误: {error_detail}")
            return jsonify({"success": False, "error": error_detail})

        base_date, base_meal_type, total_amount, _, trip_route = determine_baseline(processed_files, manual_meal_type)

        if base_date is None:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify({"success": False, "error": "无法确定基准信息，所有文件均识别失败"})

        # 按类型分组统计
        type_counts = {}
        for pf in processed_files:
            t = pf["type"]
            type_counts[t] = type_counts.get(t, 0) + 1
        logger.info(f"各类型文件统计: {type_counts}")

        output_dir = os.path.join(temp_dir, "output")
        os.makedirs(output_dir, exist_ok=True)

        type_counters = {}
        renamed_files = []

        for pf in processed_files:
            t = pf["type"]
            if t not in type_counters:
                type_counters[t] = 1
            else:
                type_counters[t] += 1

            total_of_type = type_counts.get(t, 1)
            seq = type_counters[t] if total_of_type > 1 else None

            new_name = generate_new_filename(pf, base_date, base_meal_type, total_amount, seq)
            src = pf["_saved_path"]
            dst = os.path.join(output_dir, new_name)
            shutil.copy2(src, dst)
            renamed_files.append(new_name)
            logger.info(f"重命名: {pf['_original_name']} -> {new_name}")

        # 打包 ZIP
        # 高铁类型：文件夹名 = 所有人名拼接 + "发票"
        if base_meal_type == "高铁":
            person_names = []
            for pf in processed_files:
                p = pf.get("person", "")
                if p and p not in person_names:
                    person_names.append(p)
            folder_name = "".join(person_names) + "发票" if person_names else "高铁发票"
        elif base_meal_type == "配送费":
            folder_name = "配送费发票"
        else:
            amount_zip_str = f"{int(total_amount)}" if total_amount == int(total_amount) else f"{total_amount:.2f}".rstrip("0").rstrip(".")
            folder_name = f"{base_date}-{base_meal_type}-{amount_zip_str}元"
        zip_name = folder_name + ".zip"
        zip_path = os.path.join(temp_dir, zip_name)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, filenames in os.walk(output_dir):
                for fn in filenames:
                    file_path = os.path.join(root, fn)
                    arcname = os.path.join(folder_name, fn)
                    zf.write(file_path, arcname)

        logger.info(f"ZIP 打包完成: {zip_name}")

        # 构建供飞书导出用的逐文件记录（高铁需按人分别写入）
        file_records = []
        for pf in processed_files:
            file_records.append({
                "date": pf["date"],
                "meal_type": pf["meal_type"],
                "amount": pf["amount"],
                "content": pf.get("content", ""),
                "person": pf.get("person", ""),
            })

        response_data = {
            "success": True,
            "session_id": session_id,
            "zip_filename": zip_name,
            "summary": {
                "date": base_date,
                "meal_type": base_meal_type,
                "total_amount": total_amount,
                "content": trip_route,  # 打车行程/高铁站点
            },
            "file_records": file_records,  # 逐文件详情，供高铁/打车飞书写入
            "renamed_files": renamed_files,
            "skipped_files": api_errors if api_errors else [],
        }

        return jsonify(response_data)

    except Exception as e:
        logger.error(f"处理失败: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": f"处理失败：{str(e)}"})


@app.route("/api/download/<session_id>")
def download_file(session_id):
    """通过 session_id 查找并返回 ZIP 文件，避免中文文件名编码问题"""
    temp_dir = os.path.join(tempfile.gettempdir(), f"invoice_{session_id}")
    if not os.path.exists(temp_dir):
        return jsonify({"error": "文件不存在或已过期"}), 404
    zip_files = [f for f in os.listdir(temp_dir) if f.endswith(".zip")]
    if not zip_files:
        return jsonify({"error": "文件不存在或已过期"}), 404
    zip_path = os.path.join(temp_dir, zip_files[0])
    return send_file(zip_path, as_attachment=True, download_name=zip_files[0])


@app.route("/api/cleanup/<session_id>", methods=["POST"])
def cleanup(session_id):
    temp_dir = os.path.join(tempfile.gettempdir(), f"invoice_{session_id}")
    shutil.rmtree(temp_dir, ignore_errors=True)
    return jsonify({"success": True})


@app.route("/api/get_feishu_apps", methods=["GET"])
def get_feishu_apps():
    """返回可用的飞书表格列表"""
    tables = [{"id": t["id"], "name": t["name"]} for t in FEISHU_TABLES]
    return jsonify({"success": True, "tables": tables})


@app.route("/api/export_to_feishu", methods=["POST"])
def export_to_feishu():
    """将汇总信息写入飞书多维表格（支持多表格选择，高铁类型逐人写入）"""
    try:
        data = request.get_json()
        if not data or "summary" not in data:
            return jsonify({"success": False, "error": "缺少汇总信息"})

        target = data.get("target", "dining")
        summary = data["summary"]
        file_records = data.get("file_records", [])
        logger.info(f"[飞书] 收到导出请求: target={target}, meal_type={summary.get('meal_type')}, file_records={len(file_records)}")

        if FEISHU_APP_ID == "请替换成你的飞书App ID":
            return jsonify({"success": False, "error": "请先在 main.py 中配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET"})

        # 查找对应的表格配置
        table_config = None
        for t in FEISHU_TABLES:
            if t["id"] == target:
                table_config = t
                break
        if table_config is None:
            return jsonify({"success": False, "error": f"未找到表格配置: {target}"})

        # 配送费类型写入：每条文件独立记录
        if summary.get("meal_type") == "配送费" and file_records:
            summaries = []
            for fr in file_records:
                if fr.get("meal_type") == "配送费":
                    summaries.append({
                        "date": fr.get("date", ""),
                        "meal_type": "配送费",
                        "total_amount": fr.get("amount", 0),
                    })
            logger.info(f"[飞书] 配送费类型，准备写入 {len(summaries)} 条记录")
            result = write_to_feishu_bitable(table_config, summaries)
            return jsonify({"success": True, "message": f"已导入{table_config['name']}（{len(summaries)}条）", "detail": result})

        # 高铁类型写入"其他导入"时：每人一条记录
        if target == "other" and summary.get("meal_type") == "高铁" and file_records:
            summaries = []
            for fr in file_records:
                if fr.get("meal_type") == "高铁":
                    summaries.append({
                        "date": fr.get("date", ""),
                        "meal_type": "高铁",
                        "content": fr.get("content", ""),
                        "person": fr.get("person", ""),
                        "total_amount": fr.get("amount", 0),
                    })
            logger.info(f"[飞书] 高铁类型，准备写入 {len(summaries)} 条记录")
            result = write_to_feishu_bitable(table_config, summaries)
            return jsonify({"success": True, "message": f"已导入{table_config['name']}（{len(summaries)}条）", "detail": result})

        # 普通/打车类型：写一条记录
        result = write_to_feishu_bitable(table_config, summary)
        return jsonify({"success": True, "message": f"已导入{table_config['name']}", "detail": result})

    except Exception as e:
        logger.error(f"[飞书] 导出失败: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": f"导入飞书失败：{str(e)}"})


def find_available_port(start_port=5000):
    """查找可用端口，从 start_port 开始尝试"""
    import socket

    port = start_port
    for _ in range(10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                port += 1
    return start_port


if __name__ == "__main__":
    port = find_available_port(5000)
    print(f"发票整理记账应用已启动，访问 http://127.0.0.1:{port}")
    logger.info(f"应用启动，端口: {port}")
    app.run(debug=True, host="0.0.0.0", port=port)
