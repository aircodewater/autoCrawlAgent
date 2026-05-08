import zipfile
import xml.etree.ElementTree as ET
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# 导入大模型客户端
from crawler.llm_client import get_chat_model
from langchain_core.messages import HumanMessage, SystemMessage

class ExcelReader:
    """读取Excel文件（不依赖openpyxl）"""

    def __init__(self, excel_path: str):
        self.excel_path = excel_path
        self.shared_strings = []
        self.rows = []
        self._read_excel()

    def _read_excel(self):
        """读取Excel文件"""
        with zipfile.ZipFile(self.excel_path, 'r') as zf:
            # 读取共享字符串
            if 'xl/sharedStrings.xml' in zf.namelist():
                with zf.open('xl/sharedStrings.xml') as f:
                    tree = ET.parse(f)
                    root = tree.getroot()
                    for si in root.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si'):
                        text = ''
                        for t in si.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t'):
                            if t.text:
                                text += t.text
                        self.shared_strings.append(text)

            # 读取第一个工作表
            with zf.open('xl/worksheets/sheet1.xml') as f:
                tree = ET.parse(f)
                root = tree.getroot()

                for row in root.findall('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row'):
                    row_data = []
                    for cell in row.findall('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c'):
                        cell_type = cell.get('t', '')
                        value = cell.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v')
                        if value is not None and value.text:
                            if cell_type == 's':
                                row_data.append(self.shared_strings[int(value.text)])
                            else:
                                row_data.append(value.text)
                        else:
                            row_data.append('')
                    self.rows.append(row_data)

    def get_headers(self) -> List[str]:
        """获取表头（第二行，第一行是标题说明）"""
        if len(self.rows) > 1:
            return self.rows[1]
        return []

    def get_data_rows(self) -> List[List[str]]:
        """获取数据行（从第三行开始）"""
        return self.rows[2:] if len(self.rows) > 2 else []


class EvaluationResult:
    """评估结果"""

    def __init__(self):
        self.total_fields = 0
        self.matched_fields = 0
        self.unmatched_fields = 0
        self.empty_crawl_fields = 0
        self.empty_reference_fields = 0
        self.field_details = []

    @property
    def accuracy(self) -> float:
        """计算准确率"""
        if self.total_fields == 0:
            return 0.0
        return (self.matched_fields / self.total_fields) * 100

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'total_fields': self.total_fields,
            'matched_fields': self.matched_fields,
            'unmatched_fields': self.unmatched_fields,
            'empty_crawl_fields': self.empty_crawl_fields,
            'empty_reference_fields': self.empty_reference_fields,
            'accuracy': f"{self.accuracy:.2f}%",
            'field_details': self.field_details
        }


def normalize_text(text: Optional[str]) -> str:
    """标准化文本"""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', str(text).strip())
    return text.lower()


def is_red_box_marker(text: Optional[str]) -> bool:
    """检查是否为红框内容标记"""
    if not text:
        return False
    normalized = normalize_text(text)
    return '红框' in normalized


def compare_field_values(crawl_value: str, reference_value: str, use_llm: bool = False) -> Tuple[bool, str]:
    """
    比较字段值（灵活匹配）
    返回: (是否匹配, 匹配原因)
    """
    normalized_crawl = normalize_text(crawl_value)
    normalized_reference = normalize_text(reference_value)

    # 处理爬虫值为空的情况
    if not normalized_crawl:
        if not normalized_reference:
            return True, "两者都为空"
        else:
            return False, "爬虫值为空，参考值非空"

    # 处理参考值为空的情况（如果参考值为空，无论爬虫有没有值都算对）
    if not normalized_reference:
        if not normalized_crawl:
            return True, "两者都为空"
        else:
            return True, "参考值为空，爬虫值非空（视为正确）"

    # 处理红框内容标记
    if is_red_box_marker(reference_value):
        is_match = bool(normalized_crawl)
        reason = "红框内容标记，爬虫值非空" if is_match else "红框内容标记，爬虫值为空"
        return is_match, reason

    # 精确匹配
    if normalized_crawl == normalized_reference:
        return True, "精确匹配"

    # 子字符串匹配：参考值是否在爬虫值中
    if normalized_reference in normalized_crawl:
        return True, "子字符串匹配(参考值在爬虫值中)"

    # 子字符串匹配：爬虫值是否在参考值中
    if normalized_crawl in normalized_reference:
        return True, "子字符串匹配(爬虫值在参考值中)"

    # 关键词匹配：检查参考值中的关键词是否在爬虫值中
    reference_words = normalized_reference.split()
    if len(reference_words) > 2:
        # 对于长文本，检查是否包含主要关键词
        key_words = [word for word in reference_words if len(word) > 3]
        if key_words:
            matched_keywords = [word for word in key_words if word in normalized_crawl]
            if len(matched_keywords) >= len(key_words) * 0.5:
                return True, f"关键词匹配（匹配{len(matched_keywords)}/{len(key_words)}个关键词）"

    # 数值匹配：对于包含数字的字段，检查是否有相同的数字
    reference_numbers = re.findall(r'\d+(?:\.\d+)?', normalized_reference)
    crawl_numbers = re.findall(r'\d+(?:\.\d+)?', normalized_crawl)
    if reference_numbers and crawl_numbers:
        common_numbers = set(reference_numbers) & set(crawl_numbers)
        if common_numbers:
            return True, f"数值匹配（匹配{len(common_numbers)}个数字）"

    # 简单包含匹配：检查是否包含相同的核心词汇
    if len(normalized_reference) > 5:
        # 对于较长的参考值，检查是否有部分内容匹配
        for i in range(0, len(normalized_reference), 5):
            chunk = normalized_reference[i:i+10]
            if len(chunk) > 5 and chunk in normalized_crawl:
                return True, "部分内容匹配"

    # 使用大模型评估匹配程度
    if use_llm:
        try:
            print(f"使用大模型评估字段: {crawl_value[:30]}... vs {reference_value[:30]}...")
            is_match, reason = llm_evaluate_match(crawl_value, reference_value)
            print(f"大模型评估结果: {'匹配' if is_match else '不匹配'} - {reason}")
            return is_match, f"大模型评估：{reason}"
        except Exception as e:
            print(f"大模型评估失败: {str(e)}")
            # 如果大模型评估失败，返回原值不匹配
            pass

    return False, f"值不匹配"


def llm_evaluate_match(crawl_value: str, reference_value: str) -> Tuple[bool, str]:
    """
    使用大模型评估两个值的匹配程度
    """
    print("正在初始化大模型...")
    try:
        llm = get_chat_model()
        print("大模型初始化成功")
    except Exception as e:
        print(f"大模型初始化失败: {str(e)}")
        return False, f"大模型初始化失败: {str(e)}"
    
    system_prompt = "你是一个专业的内容匹配评估专家。请评估以下两个值的匹配程度。只需返回JSON格式的结果，不要输出其他内容。"
    
    user_prompt = f"爬虫值：{crawl_value}\n参考值：{reference_value}\n\n请评估这两个值是否匹配，并提供评估理由。输出格式：{{\"is_match\": true/false, \"reason\": \"评估理由\"}}"
    
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt)
    ]
    
    print("正在调用大模型...")
    try:
        response = llm.invoke(messages)
        print("大模型调用成功")
        print(f"大模型响应: {response.content[:100]}...")
    except Exception as e:
        print(f"大模型调用失败: {str(e)}")
        return False, f"大模型调用失败: {str(e)}"
    
    # 解析大模型的输出
    try:
        import json
        response_text = response.content
        # 尝试直接解析JSON
        try:
            result = json.loads(response_text)
            return result.get('is_match', False), result.get('reason', '评估失败')
        except json.JSONDecodeError:
            # 查找JSON部分
            import re
            json_match = re.search(r'\{[\s\S]*\}', response_text)
            if json_match:
                json_str = json_match.group(0)
                result = json.loads(json_str)
                return result.get('is_match', False), result.get('reason', '评估失败')
            else:
                # 如果没有JSON，直接判断
                if 'true' in response_text.lower() or '匹配' in response_text:
                    return True, '大模型判断为匹配'
                else:
                    return False, '大模型判断为不匹配'
    except Exception as e:
        print(f"解析大模型输出失败: {str(e)}")
        return False, f'解析失败：{str(e)}'


def evaluate_crawl_results(json_path: str, excel_path: str, use_llm: bool = False) -> EvaluationResult:
    """
    评估爬虫结果与Excel参考数据的匹配度
    """
    result = EvaluationResult()

    with open(json_path, 'r', encoding='utf-8') as f:
        json_data = json.load(f)

    excel = ExcelReader(excel_path)
    headers = excel.get_headers()
    data_rows = excel.get_data_rows()

    field_name_idx = -1
    example_value_idx = -1

    for idx, header in enumerate(headers):
        header_clean = header.strip() if header else ""
        if header_clean == '字段名称':
            field_name_idx = idx
        elif header_clean == '示例值':
            example_value_idx = idx

    if field_name_idx == -1 or example_value_idx == -1:
        print(f"警告: 未找到'字段名称'或'示例值'列")
        return result

    crawl_results = json_data.get('results', {})

    for row in data_rows:
        if len(row) <= max(field_name_idx, example_value_idx):
            continue

        field_name = row[field_name_idx] if field_name_idx < len(row) else ""
        example_value = row[example_value_idx] if example_value_idx < len(row) else ""

        if not field_name:
            continue

        result.total_fields += 1

        crawl_value = crawl_results.get(field_name, "") if isinstance(crawl_results, dict) else ""

        is_match, reason = compare_field_values(crawl_value, example_value, use_llm)

        field_detail = {
            'field_name': field_name,
            'crawl_value': crawl_value[:100] + '...' if len(crawl_value) > 100 else crawl_value,
            'reference_value': example_value[:100] + '...' if len(example_value) > 100 else example_value,
            'is_match': is_match,
            'reason': reason
        }
        result.field_details.append(field_detail)

        if is_match:
            result.matched_fields += 1
        else:
            result.unmatched_fields += 1

        if not normalize_text(crawl_value):
            result.empty_crawl_fields += 1

        if not normalize_text(example_value):
            result.empty_reference_fields += 1

    return result


def print_evaluation_report(result: EvaluationResult, output_dir: Optional[str] = None):
    """打印评估报告"""
    import time
    import os
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    file_timestamp = time.strftime("%Y%m%d_%H%M%S")
    
    lines = []
    lines.append("\n" + "=" * 80)
    lines.append(f"爬虫结果评估报告 - {timestamp}")
    lines.append("=" * 80)
    lines.append("")
    lines.append(f"总字段数: {result.total_fields}")
    lines.append(f"匹配字段数: {result.matched_fields}")
    lines.append(f"不匹配字段数: {result.unmatched_fields}")
    lines.append(f"爬虫值为空的字段数: {result.empty_crawl_fields}")
    lines.append(f"参考值为空的字段数: {result.empty_reference_fields}")
    lines.append(f"准确率: {result.accuracy:.2f}%")
    lines.append("")
    lines.append("-" * 80)
    lines.append("字段详情:")
    lines.append("-" * 80)

    for detail in result.field_details:
        status = "✓" if detail['is_match'] else "✗"
        lines.append(f"{status} {detail['field_name']}")
        lines.append(f"   爬虫值: {detail['crawl_value'] or '(空)'}")
        lines.append(f"   参考值: {detail['reference_value'] or '(空)'}")
        lines.append(f"   原因: {detail['reason']}")
        lines.append("")

    report = "\n".join(lines)

    if output_dir:
        # 确保输出目录存在
        os.makedirs(output_dir, exist_ok=True)
        # 生成带时间戳的文件名
        output_path = os.path.join(output_dir, f"evaluation_report_{file_timestamp}.txt")
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"报告已保存到: {output_path}")

    print(report)


if __name__ == "__main__":
    import argparse
    import os
    
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="评估爬虫结果与Excel参考数据的匹配度")
    parser.add_argument("--json_path", type=str, help="JSON文件路径")
    parser.add_argument("--excel_path", type=str, help="Excel文件路径")
    parser.add_argument("--output_dir", type=str, help="评估报告输出目录")
    parser.add_argument("--use_llm", action="store_true", help="使用大模型评估")
    args = parser.parse_args()
    
    # 默认值
    default_json_path = r"D:\MutiAgent\AgentCrawler\results\test\test_max_iter_30\crawl_20260506_213920.json"
    default_excel_path = r"C:\Users\eic\Downloads\autoCrawlAgent-main-c284b84a67c538a92cba4f668a7de06925f12755\autoCrawlAgent\大模型爬取示例模板-多伦多大学.xlsx"
    default_output_dir = r"C:\Users\eic\Downloads\autoCrawlAgent-main-c284b84a67c538a92cba4f668a7de06925f12755\autoCrawlAgent\evaluation_report"
    
    # 使用命令行参数或默认值
    json_path = args.json_path or default_json_path
    excel_path = args.excel_path or default_excel_path
    output_dir = args.output_dir or default_output_dir
    use_llm = args.use_llm or True

    print("开始评估...")
    if use_llm:
        print("使用大模型评估匹配程度...")
    result = evaluate_crawl_results(json_path, excel_path, use_llm=use_llm)
    print_evaluation_report(result, output_dir)