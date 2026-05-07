import oci

# 查看 VnicAttachment 的属性
import inspect
print("=== VnicAttachment 属性 ===")
for name in dir(oci.core.models.VnicAttachment):
    if not name.startswith('_'):
        print(name)