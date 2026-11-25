"""出站门禁：谁允许直接调用发送层。

S1 的目标是「出口唯一」：机器人说出去的每一句都经过 outbox，由 sender 一条一条发。这条测试把
「谁可以直接摸飞书发送 API」写成**调用方白名单**，而不是去 grep ``im/v1/messages`` 字符串——
后者会禁掉 ``transport.py`` 这个共享层本身，却放过真正的调用方。

允许的两类：

1. ``bridge/service.py``：sender 与它的内部发送辅助（teach/control 回执、提醒、sender 三条路）。
2. ``bridge/cli.py`` 的两个**控制面回执**：``_notify``（私聊提醒）与 ``_send_hold_notice``
   （挂起/恢复回执）。它们必须绕过 outbox：桥接挂起时 sender 是停的，状态通知要是也排队，
   「已经挂起了」这句话永远发不出去。这是设计稿 §2 记的**唯一例外**，S2 收编。
3. ``_send_owner_notice``：第 2 条那个例外的**唯一实现**。``notify`` 与 ``task draft``
   （S2 的任务卡）都走它，所以白名单里多的是"同一例外的实现体"，不是第二个例外。
"""

import ast
from pathlib import Path
import unittest

BRIDGE = Path(__file__).resolve().parent.parent / 'bridge'

#: 发送层的方法名（``Transport`` 的公开发送面）。
SEND_METHODS = ('reply', 'send_text', 'send_dm', 'delete_message')

#: 整个文件都允许调用发送层。
ALLOWED_FILES = {'service.py'}

#: 只有这些函数允许（文件名, 顶层函数名）。控制面回执在这里显式圈出来。
ALLOWED_FUNCTIONS = {('cli.py', '_notify'), ('cli.py', '_send_hold_notice'),
                     ('cli.py', '_send_owner_notice')}

#: 发送层自己的实现，不参与扫描。
SHARED_LAYER = 'transport.py'


def find_violations(source, filename):
    """返回 ``[(行号, 方法名, 顶层函数名)]``：白名单外直接调用发送层的地方。"""
    violations = []
    stack = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in SEND_METHODS:
                top = stack[0] if stack else None
                if filename not in ALLOWED_FILES and (filename, top) not in ALLOWED_FUNCTIONS:
                    violations.append((node.lineno, func.attr, top))
            self.generic_visit(node)

    Visitor().visit(ast.parse(source, filename=filename))
    return violations


class SingleExitGateTests(unittest.TestCase):
    def test_no_send_calls_outside_the_allowed_callers(self):
        problems = []
        for path in sorted(BRIDGE.glob('*.py')):
            if path.name == SHARED_LAYER:
                continue
            for line, method, function in find_violations(path.read_text(encoding='utf-8'),
                                                          path.name):
                problems.append('%s:%s %s() 在 %s() 里直接调用发送层'
                                % (path.name, line, method, function or '<module>'))
        self.assertEqual(problems, [], '出口唯一的门禁被绕过了：\n' + '\n'.join(problems))

    def test_gate_flags_a_new_sender(self):
        source = (
            'def rogue(config, event):\n'
            '    from .transport import Transport\n'
            '    return Transport(config, event).reply({"message_id": "om_1"}, "hi", "u")\n'
        )
        violations = find_violations(source, 'metrics.py')
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0][1:], ('reply', 'rogue'))

    def test_gate_covers_every_send_method(self):
        for method in SEND_METHODS:
            with self.subTest(method=method):
                source = 'def rogue(t):\n    return t.%s()\n' % method
                self.assertEqual(len(find_violations(source, 'gateway.py')), 1)

    def test_control_plane_exceptions_are_scoped_to_their_functions(self):
        notify = 'def _notify(home, argv):\n    return t.send_dm("ou_1", "hi")\n'
        self.assertEqual(find_violations(notify, 'cli.py'), [])
        hold = 'def _send_hold_notice(config, chat, text):\n    return t.send_text(chat, text)\n'
        self.assertEqual(find_violations(hold, 'cli.py'), [])
        sneaky = 'def _other(home):\n    return t.send_text("oc_1", "hi")\n'
        self.assertEqual(len(find_violations(sneaky, 'cli.py')), 1)


if __name__ == '__main__':
    unittest.main()
