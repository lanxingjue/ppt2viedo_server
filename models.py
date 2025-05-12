# models.py
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin

# db 实例将从 app.py 导入
# 这要求 app.py 在导入 models.py 之前已经创建了 db = SQLAlchemy(app)
# 在 app.py 中，确保 'from models import User, TaskRecord' 在 'db = SQLAlchemy(app)' 之后
from app import db # <--- 确保这能正确工作

class User(UserMixin, db.Model):
    """用户模型"""
    __tablename__ = 'user'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='free')
    video_creation_limit = db.Column(db.Integer, nullable=False, default=1)
    videos_created_count = db.Column(db.Integer, nullable=False, default=0)
    registered_on = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)
    
    tasks = db.relationship('TaskRecord', backref='author', lazy='dynamic', cascade="all, delete-orphan")

    def __repr__(self):
        return f'<User {self.username}>'

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def can_create_video(self, app_config_parser): 
        if self.role == 'vip':
            limit = app_config_parser.getint('UserRoles', 'vip_video_limit', fallback=-1)
            return limit == -1 or self.videos_created_count < limit
        else: 
            limit = app_config_parser.getint('UserRoles', 'free_video_limit', fallback=1)
            return self.videos_created_count < limit
            
    def increment_video_count(self):
        if self.videos_created_count is None: self.videos_created_count = 0
        self.videos_created_count += 1


class TaskRecord(db.Model):
    """任务记录模型"""
    __tablename__ = 'task_record'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    celery_task_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    
    original_ppt_filename = db.Column(db.String(255), nullable=True)
    original_ppt_path = db.Column(db.String(512), nullable=True) # 确保这个字段存在
    
    output_video_filename = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(50), nullable=False, default='PENDING')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f'<TaskRecord {self.id} - Celery: {self.celery_task_id}>'
