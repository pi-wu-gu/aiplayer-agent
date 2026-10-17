# -*- coding: utf-8 -*-
"""工具注册表：manifest(name/description/params json-schema) + 实现函数。
参数经 jsonschema 风格手工校验（不引第三方依赖）。阶段2"""
import json


class ToolError(Exception):
    pass


class ToolRegistry:
    def __init__(self):
        self._tools = {}  # name -> {"manifest": {...}, "fn": callable, "owner": str}

    def register(self, manifest, fn, owner="builtin", replace=False):
        name = manifest.get("name")
        if not name:
            raise ToolError("manifest 缺少 name")
        if name in self._tools and not replace:
            raise ToolError("工具 %r 已注册" % name)
        if not callable(fn):
            raise ToolError("工具 %r 的实现不可调用" % name)
        if not isinstance(manifest.get("description"), str):
            raise ToolError("工具 %r 缺少 description" % name)
        params = manifest.get("parameters") or {"type": "object", "properties": {}}
        if params.get("type") != "object":
            raise ToolError("工具 %r 的 parameters.type 必须是 object" % name)
        self._tools[name] = {"manifest": {"name": name, "description": manifest["description"],
                                          "parameters": params},
                             "fn": fn, "owner": owner}

    def unregister(self, name):
        self._tools.pop(name, None)

    def names(self):
        return sorted(self._tools.keys())

    def manifests(self):
        return [self._tools[n]["manifest"] for n in self.names()]

    def get(self, name):
        return self._tools.get(name)

    @staticmethod
    def _check_value(params_schema, value, where):
        t = params_schema.get("type")
        ok_map = {"object": dict, "array": list, "string": str, "number": (int, float),
                  "integer": int, "boolean": bool, "null": type(None)}
        if t in ok_map and not isinstance(value, ok_map[t]):
            if t == "number" and isinstance(value, bool):
                raise ToolError("%s 应为 number" % where)
            if t == "integer" and isinstance(value, bool):
                raise ToolError("%s 应为 integer" % where)
            raise ToolError("%s 应为 %s，实得 %s" % (where, t, type(value).__name__))
        if t == "object":
            props = params_schema.get("properties") or {}
            for req in params_schema.get("required") or []:
                if req not in value:
                    raise ToolError("%s 缺少必填字段 %r" % (where, req))
            for k, v in value.items():
                if k in props:
                    ToolRegistry._check_value(props[k], v, "%s.%s" % (where, k))
        if t == "array" and isinstance(value, list):
            item = params_schema.get("items")
            if item:
                for i, v in enumerate(value):
                    ToolRegistry._check_value(item, v, "%s[%d]" % (where, i))
        enum = params_schema.get("enum")
        if enum and value not in enum:
            raise ToolError("%s 必须是 %s 之一，实得 %r" % (where, enum, value))

    def execute(self, name, arguments):
        """执行工具；永远返回字符串（LLM 友好）。参数校验失败/异常都转成错误文本。"""
        t = self._tools.get(name)
        if not t:
            return json.dumps({"error": "未知工具 %r；可用: %s" % (name, self.names())},
                              ensure_ascii=False)
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        except json.JSONDecodeError as e:
            return json.dumps({"error": "参数不是合法 JSON: %s" % e}, ensure_ascii=False)
        try:
            self._check_value(t["manifest"]["parameters"], args, name)
        except ToolError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        try:
            result = t["fn"](**args)
        except Exception as e:  # 工具内部异常也转文本回给 LLM
            return json.dumps({"error": "%s: %s" % (type(e).__name__, e)}, ensure_ascii=False)
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False)
        return str(result)
