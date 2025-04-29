# celery_app.py
import os
import logging
from celery import Celery
from pathlib import Path
import configparser

# --- 配置解析 ---
# 尝试加载 config.ini 文件
config = configparser.ConfigParser()
# 使用 __file__ 的父目录来确定 config.ini 的位置 (假定在项目根目录)
config_path = Path(__file__).parent / 'config.ini'
if config_path.exists():
    try:
        config.read(config_path, encoding='utf-8')
        print(f"[Celery App] 成功加载配置: {config_path}") # 启动时打印，确保配置加载
    except Exception as e:
        print(f"[Celery App] 错误: 加载配置 {config_path} 失败: {e}")
else:
    print(f"[Celery App] 警告: 配置未找到: {config_path}")

# --- 日志记录配置 ---
# Celery Worker 通常有自己的日志管理，这里配置基础日志
log_level_str = config.get('General', 'logging_level', fallback='INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)

# 注意：Celery 默认会配置日志，这里主要确保级别正确。
# 对于更复杂的日志配置（如文件输出），应该使用 Celery 的日志配置机制。
# logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - [%(process)d] - %(message)s')


# --- 创建 Celery 应用实例 ---
# 从 config.ini 的 [Celery] 部分读取 broker 和 backend URL
broker_url = config.get('Celery', 'broker_url', fallback='redis://localhost:6379/0')
result_backend = config.get('Celery', 'result_backend', fallback='redis://localhost:6379/0')

# Celery 应用名可以与项目名一致
celery_app = Celery(
    'ppt2video_tasks', # 应用名
    broker=broker_url,
    backend=result_backend,
    # --- 修改: 使用 include 参数，指定包含任务定义的模块列表 ---
    # 指定包含任务定义的模块名（tasks.py 文件名，不带 .py 后缀）
    # PyInstaller 打包时也需要确保 tasks 模块能被找到
    include=['tasks'] # <--- 使用绝对模块名 'tasks'
    # --- End 修改 ---
)

# 配置时区和任务序列化等
celery_app.conf.update(
    # 时区，根据你的需要调整
    timezone='Asia/Shanghai',
    # 任务序列化方式
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    # 任务结果过期时间（秒），例如一天
    result_expires=60*60*24,
    # 注意：这里不再需要 imports = ['tasks']，因为 include 已经指定了
    # imports=['tasks'], # 删除或注释掉

    # 添加配置对象到配置中，方便在任务中访问 (可选，不推荐直接放ConfigParser对象)
    # 更好的做法是在任务内部重新加载 config 或只传递必要的配置值
)

# 可选：为 Celery Worker 配置日志级别（由 -l 参数控制）
# celery -A celery_app worker -l info

# 如果需要，可以在这里定义一个函数来获取配置，供任务使用
# def get_celery_config():
#    # 可以在这里重新加载或返回全局 config 对象 (如果它是线程安全的)
#    return config

# --- 在 __main__ 块中定义 Worker 启动命令 (可选，更常见是在 run_worker.sh 中) ---
# if __name__ == '__main__':
#     # 这个块在直接运行 python celery_app.py 时会执行
#     # 如果使用 celery -A celery_app ... 命令，这个块不会执行
#     celery_app.start() # 启动 Celery Worker