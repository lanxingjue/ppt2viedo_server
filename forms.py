# forms.py
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, Email, EqualTo, ValidationError
from models import User # 从 models.py 导入 User 模型，用于验证唯一性

class RegistrationForm(FlaskForm):
    """用户注册表单"""
    username = StringField('用户名', 
                           validators=[DataRequired(message="请输入用户名。"), 
                                       Length(min=3, max=20, message="用户名长度应在3到20个字符之间。")])
    email = StringField('邮箱地址',
                        validators=[DataRequired(message="请输入邮箱地址。"), 
                                    Email(message="请输入有效的邮箱地址。")])
    password = PasswordField('密码', 
                             validators=[DataRequired(message="请输入密码。"), 
                                         Length(min=6, message="密码长度至少为6位。")])
    confirm_password = PasswordField('确认密码',
                                     validators=[DataRequired(message="请再次输入密码。"), 
                                                 EqualTo('password', message="两次输入的密码不一致。")])
    submit = SubmitField('注册')

    def validate_username(self, username):
        """验证用户名是否已存在"""
        user = User.query.filter_by(username=username.data).first()
        if user:
            raise ValidationError('该用户名已被注册，请选择其他用户名。')

    def validate_email(self, email):
        """验证邮箱是否已存在"""
        user = User.query.filter_by(email=email.data).first()
        if user:
            raise ValidationError('该邮箱已被注册，请使用其他邮箱。')


class LoginForm(FlaskForm):
    """用户登录表单"""
    email = StringField('邮箱地址',
                        validators=[DataRequired(message="请输入邮箱地址。"), 
                                    Email(message="请输入有效的邮箱地址。")])
    password = PasswordField('密码', validators=[DataRequired(message="请输入密码。")])
    remember = BooleanField('记住我') # “记住我”功能
    submit = SubmitField('登录')

