# models.py
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin # UserMixin 提供了 Flask-Login 需要的默认实现
from app import db, login_manager # 从 app.py 导入 db 和 login_manager 实例

# Flask-Login 需要一个 user_loader 函数来从会话中加载用户
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

class User(db.Model, UserMixin):
    """用户模型"""
    __tablename__ = 'user' # 定义表名

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False) # 增加密码哈希长度
    role = db.Column(db.String(20), nullable=False, default='free')  # e.g., 'free', 'vip'
    
    # 从 config.ini 读取默认限制，或在此处硬编码
    # 这里我们先用硬编码的思路，后续可以改为从配置读取
    video_creation_limit = db.Column(db.Integer, nullable=False, default=1) # 免费用户默认1次
    videos_created_count = db.Column(db.Integer, nullable=False, default=0)
    
    registered_on = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True) # 上次登录时间

    # 定义与 TaskRecord 的一对多关系
    # backref='author' 允许我们通过 TaskRecord.author 访问关联的 User 对象
    # lazy=True 表示 SQLAlchemy 将在需要时才从数据库加载相关对象
    tasks = db.relationship('TaskRecord', backref='author', lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f'<User {self.username} ({self.email}) - Role: {self.role}>'

    def set_password(self, password):
        """设置密码，存储哈希值"""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """检查密码是否匹配"""
        return check_password_hash(self.password_hash, password)

    def can_create_video(self, config): # 传入配置对象
        """检查用户是否还有创建视频的额度"""
        if self.role == 'vip':
            # VIP 用户可以从配置中读取限制，或默认为无限
            limit = config.getint('UserRoles', 'vip_video_limit', fallback=-1)
            return limit == -1 or self.videos_created_count < limit
        else: # 免费用户
            limit = config.getint('UserRoles', 'free_video_limit', fallback=1)
            return self.videos_created_count < limit
            
    def increment_video_count(self):
        """增加用户已创建视频计数"""
        if self.videos_created_count is None: # 处理可能的 None 值
            self.videos_created_count = 0
        self.videos_created_count += 1


class TaskRecord(db.Model):
    """任务记录模型"""
    __tablename__ = 'task_record' # 定义表名

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False) # 外键关联到 User 表的 id 字段
    celery_task_id = db.Column(db.String(120), unique=True, nullable=False, index=True) # Celery 任务 ID
    
    original_ppt_filename = db.Column(db.String(255), nullable=True)
    output_video_filename = db.Column(db.String(255), nullable=True) # 生成的视频文件名 (不含路径)
    # output_video_relative_path = db.Column(db.String(512), nullable=True) # 相对路径，方便前端构造下载链接

    status = db.Column(db.String(50), nullable=False, default='PENDING') # 任务状态
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True) # 存储错误信息
    
    # 注意：与 User 模型的反向关系 'author' 是通过 User.tasks 定义的
    # user = db.relationship('User', backref=db.backref('task_records', lazy=True)) # 另一种定义关系的方式

    def __repr__(self):
        return f'<TaskRecord {self.id} (CeleryID: {self.celery_task_id}) - User: {self.user_id} - Status: {self.status}>'

