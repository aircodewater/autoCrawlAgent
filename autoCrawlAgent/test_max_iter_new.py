import os
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import sys

# 测试参数设置
start_iter = 30
step_iter = 5
max_target = 63
max_iter_limit = 30 # 最大--max-iter值
output_dir = r"D:\MutiAgent\AgentCrawler\results\test"
url = "https://future.utoronto.ca/"
topic_file = "topic"
# True：子进程 main.py 会加载 crawl_runtime.json（natural_language_search、export_url_tree 等）
# False：完全忽略 crawl_runtime.json，需自行在 cmd 里加 --enable-search / --no-url-tree 等
use_runtime_config = True
result_file = "max_iter_test_results.txt"

# 进程锁文件路径
lock_file = os.path.join(output_dir, "test_running.lock")

# 检查是否已有测试在运行
if os.path.exists(lock_file):
    print("错误: 检测到有测试正在运行或上次运行未正常退出")
    print("请先关闭所有正在运行的测试脚本")
    print(f"锁文件位置: {lock_file}")
    print("删除锁文件后重试")
    sys.exit(1)

# 创建锁文件
with open(lock_file, 'w') as f:
    f.write(f"{os.getpid()}\n")
    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n")

print(f"测试进程 PID: {os.getpid()}")

# 确保输出目录存在
if not os.path.exists(output_dir):
    os.makedirs(output_dir)
    print(f"创建输出目录: {output_dir}")

# 清空结果文件（确保只有一次运行的数据）
with open(result_file, 'w', encoding='utf-8') as f:
    f.write("# --max-iter 测试结果\n")
    f.write("# 格式: max_iter, 非空字段数, 总字段数\n")
    f.write("# 开始测试时间: " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
    f.write("\n")
print(f"已清空结果文件: {result_file}")
if use_runtime_config:
    print(
        "子进程将加载 crawl_runtime.json（export_url_tree、natural_language_search 等）；"
        "命令行上的 url / -o / --max-iter 仍会覆盖 JSON 里同名字段"
    )
else:
    print("子进程使用 --no-runtime-config，不读取 crawl_runtime.json")

# 记录结果
results = []
results_lock = threading.Lock()

# 测试单个max_iter值的函数
def test_single_max_iter(current_iter, url, output_dir, result_file, max_target):
    """测试单个max_iter值"""
    print(f"\n测试 --max-iter={current_iter}")
    print("-" * 40)
    
    # 为每个测试创建单独的输出目录
    test_output_dir = os.path.join(output_dir, f"test_max_iter_{current_iter}")
    if not os.path.exists(test_output_dir):
        os.makedirs(test_output_dir)
        print(f"创建测试输出目录: {test_output_dir}")
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cmd = [
        sys.executable,
        "-u",
        "main.py",
        url,
        "--topic-file",
        topic_file,
        "-o",
        test_output_dir,
        f"--max-iter={current_iter}",
    ]
    if not use_runtime_config:
        cmd.insert(3, "--no-runtime-config")
    print(f"执行命令: {' '.join(cmd)}")
    
    try:
        print("正在执行爬虫（实时输出；stderr 已合并到 stdout）...")
        # Windows 子进程 stdout 接管道时默认常用 GBK，父进程若按 UTF-8 解码会乱码
        child_env = os.environ.copy()
        child_env.setdefault("PYTHONUTF8", "1")
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        # 勿使用 capture_output=True，否则子进程日志在结束前不可见
        process = subprocess.Popen(
            cmd,
            cwd=script_dir,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
        return_code = process.wait()
        
        if return_code != 0:
            print(f"执行失败，返回码: {return_code}")
            return {'max_iter': current_iter, 'non_empty_fields': -1, 'total_fields': -1, 'status': '执行失败'}
        
        print("爬虫执行成功")
        # 等待5秒，确保文件写入完成
        time.sleep(5)
        
        # 查找主爬取结果 JSON（勿用 *_url_tree.json：无 results，会导致统计全 0）
        print("查找JSON结果文件...")
        try:
            all_json = [f for f in os.listdir(test_output_dir) if f.endswith(".json")]
        except Exception as e:
            print(f"读取目录失败: {str(e)}")
            return {'max_iter': current_iter, 'non_empty_fields': -1, 'total_fields': -1, 'status': '读取目录失败'}
        
        json_files = [
            f
            for f in all_json
            if not f.endswith("_url_tree.json") and not f.endswith("_url_fields.json")
        ]
        crawl_main = [f for f in json_files if f.startswith("crawl_")]
        if crawl_main:
            json_files = crawl_main
        if not json_files:
            json_files = all_json
        if not json_files:
            print("错误: 未找到JSON结果文件")
            return {'max_iter': current_iter, 'non_empty_fields': -1, 'total_fields': -1, 'status': '未找到JSON文件'}
        
        json_files.sort(key=lambda x: os.path.getmtime(os.path.join(test_output_dir, x)), reverse=True)
        latest_json = os.path.join(test_output_dir, json_files[0])
        print(f"用于统计的JSON文件: {latest_json}")
        
        # 分析结果
        print("分析结果...")
        try:
            with open(latest_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            print(f"读取JSON文件失败: {str(e)}")
            return {'max_iter': current_iter, 'non_empty_fields': -1, 'total_fields': -1, 'status': '读取JSON失败'}
            
        sc = data.get("spider_canada")
        if isinstance(sc, dict) and sc:
            results_dict = sc
        else:
            results_dict = data.get("results")
        if not isinstance(results_dict, dict):
            results_dict = {}
        def _filled(v):
            if v is None:
                return False
            if isinstance(v, str):
                return bool(v.strip())
            return bool(v)

        non_empty_fields = sum(1 for v in results_dict.values() if _filled(v))
        total_fields = len(results_dict)
        
        print(f"总字段数: {total_fields}")
        print(f"非空字段数: {non_empty_fields}")
        
        # 检查是否达到目标
        if non_empty_fields >= max_target:
            print(f"\n✓ 达到目标: 非空字段数 = {non_empty_fields} ≥ {max_target}")
            return {'max_iter': current_iter, 'non_empty_fields': non_empty_fields, 'total_fields': total_fields, 'status': '达到目标'}
        
        return {'max_iter': current_iter, 'non_empty_fields': non_empty_fields, 'total_fields': total_fields, 'status': '完成'}
        
    except Exception as e:
        print(f"执行错误: {str(e)}")
        return {'max_iter': current_iter, 'non_empty_fields': -1, 'total_fields': -1, 'status': f'错误: {str(e)}'}

print("开始测试不同的--max-iter值...")
print("=" * 80)
print(f"测试参数: start_iter={start_iter}, step_iter={step_iter}, max_target={max_target}, max_iter_limit={max_iter_limit}")
print(f"输出目录: {output_dir}")
print(f"结果文件: {result_file}")
print("=" * 80)

# 生成所有要测试的max_iter值
test_values = []
current_iter = start_iter
while current_iter <= max_iter_limit:
    test_values.append(current_iter)
    current_iter += step_iter

print(f"生成测试值（未过滤）: {test_values}")
print(f"max_iter_limit: {max_iter_limit}")

# 确保测试值中没有超过max_iter_limit的
test_values = [val for val in test_values if val <= max_iter_limit]

print(f"共需测试 {len(test_values)} 个max_iter值")
print(f"测试值: {test_values}")

# 验证测试值
if not test_values:
    print("错误: 没有有效的测试值！")
    exit(1)

if any(val > max_iter_limit for val in test_values):
    print("错误: 测试值中存在超过max_iter_limit的值！")
    exit(1)

# 使用线程池并行测试（根据CPU核心数设置合理的线程数）
cpu_count = os.cpu_count() or 4
max_workers = min(max(2, min(2, cpu_count)), len(test_values))  # 最多10个线程，但不超过CPU核心数和测试值数量
print(f"CPU核心数: {cpu_count}")
print(f"最大线程数: {max_workers}")

# 批处理参数设置
batch_size = 2  # 每批次执行的任务数
print(f"批处理设置: 每批次 {batch_size} 个任务")

# 分组处理
batches = []
for i in range(0, len(test_values), batch_size):
    batch = test_values[i:i + batch_size]
    batches.append(batch)

print(f"共分为 {len(batches)} 个批次")
for i, batch in enumerate(batches):
    print(f"批次 {i+1}: {batch}")

# 按批次执行
for i, batch in enumerate(batches):
    print(f"\n执行第 {i+1} 批次，包含任务: {batch}")
    print("-" * 60)
    
    # 使用线程池执行当前批次的任务
    current_workers = min(len(batch), max_workers)
    print(f"当前批次使用 {current_workers} 个线程")
    
    with ThreadPoolExecutor(max_workers=current_workers) as executor:
        # 提交当前批次的任务
        future_to_iter = {executor.submit(test_single_max_iter, iter_val, url, output_dir, result_file, max_target): iter_val for iter_val in batch}
        
        # 收集结果
        for future in as_completed(future_to_iter):
            iter_val = future_to_iter[future]
            try:
                result = future.result()
                with results_lock:
                    results.append(result)
                print(f"测试 --max-iter={iter_val} 完成: {result['status']}")
            except Exception as e:
                print(f"测试 --max-iter={iter_val} 异常: {str(e)}")
                with results_lock:
                    results.append({'max_iter': iter_val, 'non_empty_fields': -1, 'total_fields': -1, 'status': f'异常: {str(e)}'})
    
    # 批次间增加延迟
    if i < len(batches) - 1:
        print("等待5秒后执行下一批次...")
        time.sleep(5)

# 汇总结果
# 按max_iter值排序结果
sorted_results = sorted(results, key=lambda x: x['max_iter'])

print(f"测试完成，共 {len(sorted_results)} 个结果")
print(f"排序后结果: {[r['max_iter'] for r in sorted_results]}")

# 重新写入结果文件（按顺序）
# 使用排他锁确保文件写入安全
with results_lock:
    # 清空文件并重新写入
    with open(result_file, 'w', encoding='utf-8') as f:
        f.write("# --max-iter 测试结果\n")
        f.write("# 格式: max_iter, 非空字段数, 总字段数\n")
        f.write("# 开始测试时间: " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
        f.write("\n")
        for result in sorted_results:
            if result['non_empty_fields'] != -1:
                f.write(f"{result['max_iter']}, {result['non_empty_fields']}, {result['total_fields']}\n")
            else:
                f.write(f"{result['max_iter']}, -1, -1, {result['status']}\n")

print(f"结果已写入文件: {result_file}")

print("\n" + "=" * 80)
print("测试结果汇总")
print("=" * 80)
print("| max_iter | 非空字段数 | 总字段数 | 状态 |")
print("|----------|------------|----------|------|")
for result in sorted_results:
    print(f"| {result['max_iter']:8} | {result['non_empty_fields']:10} | {result['total_fields']:8} | {result.get('status', '未知')[:10]} |")

print("\n" + "=" * 80)
print("最优--max-iter值分析:")
print("=" * 80)

if results:
    # 找到第一个达到目标的max_iter
    optimal_result = None
    for result in sorted_results:
        if result['non_empty_fields'] >= max_target and result['non_empty_fields'] != -1:
            optimal_result = result
            break
    
    if optimal_result:
        print(f"最优--max-iter值: {optimal_result['max_iter']}")
        print(f"对应的非空字段数: {optimal_result['non_empty_fields']}")
        print(f"总字段数: {optimal_result['total_fields']}")
        # 记录完成信息
        with results_lock:
            with open(result_file, 'a', encoding='utf-8') as f:
                f.write(f"\n# 达到目标，测试完成\n")
                f.write(f"# 最优--max-iter值: {optimal_result['max_iter']}\n")
                f.write(f"# 非空字段数: {optimal_result['non_empty_fields']}\n")
    else:
        print("未达到目标字段数")
        # 找出非空字段数最多的结果
        valid_results = [r for r in results if r['non_empty_fields'] != -1]
        if valid_results:
            best_result = max(valid_results, key=lambda x: x['non_empty_fields'])
            print(f"\n最佳结果:")
            print(f"max_iter: {best_result['max_iter']}")
            print(f"非空字段数: {best_result['non_empty_fields']}")
            print(f"总字段数: {best_result['total_fields']}")
            # 记录最佳结果
            with results_lock:
                with open(result_file, 'a', encoding='utf-8') as f:
                    f.write(f"\n# 未达到目标，最佳结果\n")
                    f.write(f"# max_iter: {best_result['max_iter']}\n")
                    f.write(f"# 非空字段数: {best_result['non_empty_fields']}\n")
else:
    print("未获取到测试结果")

# 记录测试结束时间
with results_lock:
    with open(result_file, 'a', encoding='utf-8') as f:
        f.write(f"\n# 测试结束时间: " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n")

print(f"\n测试结果已保存到: {result_file}")
print("测试完成！")

# 删除锁文件
try:
    if os.path.exists(lock_file):
        os.remove(lock_file)
        print(f"已删除锁文件: {lock_file}")
except Exception as e:
    print(f"删除锁文件失败: {str(e)}")
