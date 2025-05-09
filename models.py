# models.py
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
# 从 app.py 导入 db 和 login_manager 实例
# 这要求 app.py 中已经创建了这些实例，并且 celery_utils.py 能正确导入 app
# 为了避免直接的循环依赖，通常 login_manager.user_loader 会在 app.py 中定义
# 这里我们假设 login_manager 实例在 app.py 中定义并传递给 UserMixin

# 尝试从 app 模块导入 db 和 login_manager
# 这将在 app.py 完成其基本初始化后进行
_db_for_model = None
_login_manager_for_model = None

def init_models_dependencies(db_instance, login_manager_instance):
    """在 app.py 中调用以传递 db 和 login_manager 实例"""
    global _db_for_model, _login_manager_for_model
    _db_for_model = db_instance
    _login_manager_for_model = login_manager_instance

    # Flask-Login 需要一个 user_loader 函数来从会话中加载用户
    @_login_manager_for_model.user_loader
    def load_user(user_id):
        # 确保 User 类已定义
        return User.query.get(int(user_id))


class User(UserMixin): # UserMixin 提供了 Flask-Login 需要的默认实现
    """用户模型"""
    # __tablename__ = 'user' # SQLAlchemy 会自动从小写类名推断

    # 确保在类定义时 _db_for_model 已经被 init_models_dependencies 设置
    # 或者，更常见的做法是在 app.py 中定义模型，并从那里导入
    # 这里我们假设 _db_for_model 会在 User 类被 SQLAlchemy 使用前被设置
    if _db_for_model:
        id = _db_for_model.Column(_db_for_model.Integer, primary_key=True)
        username = _db_for_model.Column(_db_for_model.String(80), unique=True, nullable=False)
        email = _db_for_model.Column(_db_for_model.String(120), unique=True, nullable=False)
        password_hash = _db_for_model.Column(_db_for_model.String(256), nullable=False)
        role = _db_for_model.Column(_db_for_model.String(20), nullable=False, default='free')
        video_creation_limit = _db_for_model.Column(_db_for_model.Integer, nullable=False, default=1)
        videos_created_count = _db_for_model.Column(_db_for_model.Integer, nullable=False, default=0)
        registered_on = _db_for_model.Column(_db_for_model.DateTime, nullable=False, default=datetime.utcnow)
        last_login = _db_for_model.Column(_db_for_model.DateTime, nullable=True)
        tasks = _db_for_model.relationship('TaskRecord', backref='author', lazy=True, cascade="all, delete-orphan")
    else:
        # 如果 _db_for_model 未设置，这会导致错误，但这是为了让代码能被解析
        # 实际使用时，init_models_dependencies 必须先被调用
        pass


    def __repr__(self):
        return f'<User {self.username} ({self.email}) - Role: {self.role}>'

    # UserMixin 提供了 get_id() 方法，所以不需要显式定义
    # def get_id(self):
    # return str(self.id)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def can_create_video(self, app_config): # app_config 是 configparser 对象
        if self.role == 'vip':
            limit = app_config.getint('UserRoles', 'vip_video_limit', fallback=-1)
            return limit == -1 or self.videos_created_count < limit
        else:
            limit = app_config.getint('UserRoles', 'free_video_limit', fallback=1)
            return self.videos_created_count < limit
            
    def increment_video_count(self):
        if self.videos_created_count is None:
            self.videos_created_count = 0
        self.videos_created_count += 1


class TaskRecord:
    """任务记录模型"""
    # __tablename__ = 'task_record'
    if _db_for_model:
        id = _db_for_model.Column(_db_for_model.Integer, primary_key=True)
        user_id = _db_for_model.Column(_db_for_model.Integer, _db_for_model.ForeignKey('user.id'), nullable=False)
        celery_task_id = _db_for_model.Column(_db_for_model.String(120), unique=True, nullable=False, index=True)
        original_ppt_filename = _db_for_model.Column(_db_for_model.String(255), nullable=True)
        original_ppt_path = _db_for_model.Column(_db_for_model.String(512), nullable=True) # 新增：存储原始PPT的相对路径
        output_video_filename = _db_for_model.Column(_db_for_model.String(255), nullable=True)
        status = _db_for_model.Column(_db_for_model.String(50), nullable=False, default='PENDING')
        created_at = _db_for_model.Column(_db_for_model.DateTime, nullable=False, default=datetime.utcnow)
        completed_at = _db_for_model.Column(_db_for_model.DateTime, nullable=True)
        error_message = _db_for_model.Column(_db_for_model.Text, nullable=True)
    else:
        pass

    def __repr__(self):
        return f'<TaskRecord {self.id} (CeleryID: {self.celery_task_id}) - User: {self.user_id} - Status: {self.status}>'

# --- 在 app.py 中，你需要这样做 ---
# from flask import Flask
# from flask_sqlalchemy import SQLAlchemy
# from flask_login import LoginManager
# app = Flask(__name__)
# # ... 配置 app ...
# db = SQLAlchemy(app)
# login_manager = LoginManager(app)
#
# from models import init_models_dependencies, User, TaskRecord
# init_models_dependencies(db, login_manager) # 传递实例
#
# # 然后 User 和 TaskRecord 类就可以正确地使用 db.Column 等
# # 并且 load_user 装饰器也能正确工作

