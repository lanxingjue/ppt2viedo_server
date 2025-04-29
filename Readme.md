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
