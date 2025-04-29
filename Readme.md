ppt2video_web_service/
├── app.py                 # Flask 应用主文件 (处理网页请求、启动任务)
├── tasks.py               # Celery 任务定义 (包含核心转换逻辑)
├── celery_app.py          # Celery 应用实例配置
├── requirements.txt       # Python 依赖列表
├── config.ini             # 配置文件 (路径、模型等)
├── templates/
│   ├── index.html         # 主上传页面模板
│   └── status.html        # 任务状态显示页面模板
├── static/
│   ├── css/
│   │   └── style.css      # 霓虹/赛博朋克风格 CSS
│   ├── js/
│   │   └── status_check.js # 用于轮询任务状态的 JS
│   └── assets/              # 存放 logo、背景图等静态资源 (可选)
│       └── app_icon.png     # 示例图标 (或其他格式)
├── uploads/               # 存储用户上传的 PPTX 文件 (运行时创建)
├── output/                # 存储生成的最终视频文件 (运行时创建)
├── processing_temp/       # 存储每个任务的临时文件 (运行时创建)
├── core_logic/            # 将原有的处理逻辑模块放入子目录，方便管理
│   ├── __init__.py
│   ├── ppt_processor.py
│   ├── video_synthesizer.py
│   ├── tts_manager_edge.py
│   └── ppt_exporter_libreoffice.py # 只保留 LibreOffice 导出器
└── run_worker.sh          # 启动 Celery worker 的脚本 (示例)
└── README_WEB.md          # 新的项目说明

第五步：启动 Celery Worker
cd ppt2video_web_service # 确保在项目根目录
source .venv/bin/activate # 激活虚拟环境

# 启动 Worker
# -A celery_app: 指定 Celery 应用实例所在的模块 (celery_app.py)
# worker: 启动 worker 进程
# -l info: 设置日志级别为 info，方便查看过程和错误
# -P gevent 或 -P eventlet (可选): 如果使用了异步库（如 edge-tts 依赖的 aiohttp），
# 并且在任务中直接调用了 run_async_in_sync 这样的函数，
# 最好使用 gevent 或 eventlet 进程池来提高并发处理能力。
# 如果使用它们，你需要安装相应的库: pip install gevent 或 pip install eventlet
celery -A celery_app worker -l info


第六步：启动 Flask Web 服务器
cd ppt2video_web_service # 确保在项目根目录
source .venv/bin/activate # 激活虚拟环境

# 开发模式 (用于开发和测试，不适合生产环境)
# --host=0.0.0.0 允许从外部访问
# --port=5000 设置端口
python -m flask run --host=0.0.0.0 --port=5000

# 生产模式 (推荐使用 Gunicorn 或 uWSGI)
# 先安装 Gunicorn: pip install gunicorn
# --workers 4: 启动 4 个 worker 进程 (根据服务器核心数调整)
# --bind 0.0.0.0:8000: 绑定到所有接口和 8000 端口
# app:app: 指定 Flask 应用实例 (在 app.py 文件中的 app 变量)
# gunicorn --workers 4 --bind 0.0.0.0:8000 app:app