import os
# 读取文件
with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 替换中文
replacements = {
    'Home': '首页',
    '+ Add Tenant': '+ 添加租户',
    'Users': '用户管理',
    'Instances': '实例',
    'Security Groups': '安全组',
    'Tenants': '租户列表',
    'Add Tenant': '添加租户',
    'Tenant Name': '租户名称',
    'Region': '区域',
    'Tenant OCID': '租户 OCID',
    'User OCID': '用户 OCID',
    'API Fingerprint': 'API 指纹',
    'API Key File': 'API 密钥文件',
    'Save': '保存',
    'Created': '创建时间',
    'Delete?': '确认删除?',
    'Del': '删除',
    'No tenants': '暂无租户',
    'Users - ': '用户管理 - ',
    'Create User': '创建用户',
    'Username': '用户名',
    'Email': '邮箱',
    'Admin': '管理员',
    'Password': '密码',
    'Create': '创建',
    'Reset MFA?': '确认重置 MFA?',
    'Reset MFA': '重置MFA',
    'MFA': 'MFA状态',
    'Yes': '是',
    'No': '否',
    'Actions': '操作',
    'Instances - ': '实例列表 - ',
    'Name': '名称',
    'Shape': '规格',
    'Status': '状态',
    'Public IP': '公网IP',
    'Start': '启动',
    'Stop': '停止',
    'Change IP': '换IP',
    'No instances': '暂无实例',
    'Change IP - ': '换IP - ',
    'Current IP': '当前IP',
    'N/A': '无',
    'Note:': '注意：',
    'Release current IP and get new one.': '将释放当前IP并分配新IP',
    'Change IP?': '确认换IP?',
    'Cancel': '取消',
    'Security Lists - ': '安全组列表 - ',
    'VCN': 'VCN',
    'Rules': '规则',
    'View/Add': '查看/添加',
    'No security lists': '暂无安全组',
    'Security List: ': '安全组：',
    'Security Lists': '安全组列表',
    'Ingress': '入站规则',
    'Egress': '出站规则',
    'Protocol': '协议',
    'Source CIDR': '源 CIDR',
    'Destination CIDR': '目标 CIDR',
    'Port Min/Max': '端口范围',
    'Add': '添加',
    'Source': '来源',
    'Destination': '目标',
    'Port': '端口',
    'Oracle Cloud Management': '甲骨文云管理助手'
}

for en, cn in replacements.items():
    content = content.replace(en, cn)

# 写回文件
with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Done!')