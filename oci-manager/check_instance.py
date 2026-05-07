import oci
import inspect

# 获取 Instance 类的属性
print("=== Instance 类的主要属性 ===")
for name, obj in inspect.getmembers(oci.core.models.Instance):
    if not name.startswith('_'):
        print(f"{name}: {type(obj)}")

print("\n=== 检查 vnic 相关的属性 ===")
# 查找包含 vnic 的属性
attrs = [a for a in dir(oci.core.models.Instance) if 'vnic' in a.lower()]
print(attrs)