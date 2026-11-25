"""回复正文的最后一道工序：署名、脱敏、长度上限（唯一实现 = bridge/reply.py）。

内置 analyst 删除后，这层仍被两处使用：`feishu-bridge doctor` 要打印"最终会发出去的那行
署名"，dsh 消费者在提交正文前要按同一判据脱敏。所以它单独一份、单独测。
"""
import unittest

from bridge.reply import (MAX_REPLY_CHARS, MAX_SIGNATURE_LENGTH, NEUTRAL_SIGNATURE_OWNER,
                          OWNER_PLACEHOLDER, SIGNATURE, normalize_signature,
                          redact_secret_reply, render_reply, resolve_signature)


class SignatureTests(unittest.TestCase):
    def test_builtin_signature_carries_no_person_name(self):
        """事故回归：兜底署名一度写死仓库作者的名字，同事机装完就对外自称「我是某某的 AI 助手」。

        兜底值是**模板**（只有 {owner} 占位符），账号名渲染时才进来；取不到身份时回落成
        中性词——代码里永远不存在一个具体人名。
        """
        self.assertIn(OWNER_PLACEHOLDER, SIGNATURE)
        self.assertEqual(render_reply('答案'), '答案\n此回复信息来自账号本人的 AI 助手')
        self.assertTrue(render_reply('答案').endswith('\n' + resolve_signature(None, None)))
        self.assertIn(NEUTRAL_SIGNATURE_OWNER, resolve_signature(None, None))

    def test_signature_owner_placeholder_renders_the_account_name(self):
        """署名里的 {owner} 由本机账号姓名渲染：和正文自称同一个来源，不是配置里的一份副本。"""
        self.assertEqual(resolve_signature(None, '邹远辉'), '此回复信息来自邹远辉的 AI 助手')
        self.assertEqual(render_reply('答案', None, owner='邹远辉'),
                         '答案\n此回复信息来自邹远辉的 AI 助手')
        self.assertEqual(resolve_signature(' 来自 {owner} 的AI助手 ', '李四'), '来自 李四 的AI助手')
        # 取不到身份 → 中性词，绝不回落成任何人名
        self.assertEqual(resolve_signature('来自{owner}的AI助手', None), '来自账号本人的AI助手')
        self.assertEqual(resolve_signature('来自{owner}的AI助手', '   '), '来自账号本人的AI助手')
        self.assertEqual(resolve_signature(None, 0), '此回复信息来自账号本人的 AI 助手')
        # 不含占位符的旧署名（Windows 机器上显式配的那种）字面值一字不改
        self.assertEqual(resolve_signature('来自小明的AI助手', '邹远辉'), '来自小明的AI助手')

    def test_signature_is_set_by_the_installer_not_the_code(self):
        custom = '来自小明的AI助手'
        rendered = render_reply('答案', custom)
        self.assertEqual(rendered, '答案\n' + custom)
        self.assertNotIn(custom, resolve_signature(None, None))
        # 已经带着自定义署名的正文不会重复追加
        self.assertEqual(render_reply('答案\n' + custom, custom), '答案\n' + custom)
        self.assertEqual(render_reply('答案', '  来自   小明 的AI助手 '), '答案\n来自 小明 的AI助手')
        self.assertEqual(len(normalize_signature('x' * 200)), MAX_SIGNATURE_LENGTH)
        default = resolve_signature(None, None)
        self.assertEqual(render_reply('答案'), '答案\n' + default)
        self.assertEqual(render_reply('答案', ''), '答案\n' + default)
        self.assertEqual(render_reply('答案', None), '答案\n' + default)
        long_reply = render_reply('a' * 7000, custom)
        self.assertEqual(len(long_reply), MAX_REPLY_CHARS)
        self.assertTrue(long_reply.endswith('\n' + custom))

    def test_render_signature_and_limit(self):
        default = resolve_signature(None, None)
        self.assertEqual(render_reply('2\n' + default + '\n' + default), '2\n' + default)
        leaked = render_reply('密钥是 sk-' + ('a' * 24))
        self.assertNotIn('sk-', leaked)
        self.assertIn('已拦截', leaked)
        rendered = render_reply('a' * 7000)
        self.assertEqual(len(rendered), MAX_REPLY_CHARS)
        self.assertTrue(rendered.endswith('\n' + default))
        with self.assertRaises(ValueError):
            render_reply('  ')


class MentionTests(unittest.TestCase):
    """群里回复 @ 提问的人，绝不 @ 本人（2026-11-10 本人：「肯定不能@我吧」）。"""

    def test_the_asker_is_mentioned_first(self):
        rendered = render_reply('好的', '署名', mention='ou_peer')
        self.assertEqual(rendered, '<at user_id="ou_peer"></at> 好的\n署名')

    def test_a_mention_of_the_owner_is_always_removed(self):
        rendered = render_reply('<at user_id="ou_owner"></at> 收到', '署名',
                                mention='ou_peer', self_open_id='ou_owner')
        # 提示里 @ 了本人，也不许他把自己的账号 @ 一遍
        self.assertNotIn('ou_owner', rendered)
        self.assertEqual(rendered, '<at user_id="ou_peer"></at> 收到\n署名')

    def test_placeholders_never_reach_the_group(self):
        rendered = render_reply('@_user_1 收到，@_user_2 稍等', '署名')
        self.assertNotIn('@_user_', rendered)
        self.assertEqual(rendered, '收到，稍等\n署名')

    def test_private_chat_gets_no_mention(self):
        self.assertEqual(render_reply('好的', '署名'), '好的\n署名')
        self.assertEqual(render_reply('好的', '署名', mention=None), '好的\n署名')
        # 不给 id 就不加：宁可不 @，也不要凭空造一个
        self.assertEqual(render_reply('好的', '署名', mention='@_user_1'), '好的\n署名')

    def test_mention_of_the_owner_alone_is_never_added(self):
        rendered = render_reply('好的', '署名', mention='ou_owner', self_open_id='ou_owner')
        self.assertEqual(rendered, '好的\n署名')


class ReceiptTests(unittest.TestCase):
    """回执排在署名后面：落款在前、附注在后（2026-11-06 本人指出署名夹在回执中间）。"""

    def test_the_receipt_goes_after_the_signature(self):
        rendered = render_reply('正文', '来自小明的AI助手', receipt='· 记在「风格」· 汇报先给结论')
        self.assertEqual(rendered.splitlines(),
                         ['正文', '来自小明的AI助手', '', '· 记在「风格」· 汇报先给结论'])

    def test_no_receipt_means_no_trailing_blank_line(self):
        self.assertEqual(render_reply('正文', '署名'), '正文\n署名')
        self.assertEqual(render_reply('正文', '署名', receipt='   '), '正文\n署名')

    def test_a_secret_in_the_receipt_is_redacted_too(self):
        rendered = render_reply('正文', '署名', receipt='· key sk-' + 'a' * 24)
        self.assertNotIn('sk-', rendered)

    def test_an_over_long_body_is_cut_but_the_signature_and_receipt_survive(self):
        rendered = render_reply('a' * 7000, '署名', receipt='· 记在「风格」· 汇报先给结论')
        self.assertEqual(len(rendered), MAX_REPLY_CHARS)
        self.assertIn('署名', rendered)
        self.assertTrue(rendered.endswith('· 记在「风格」· 汇报先给结论'))


class RedactionTests(unittest.TestCase):
    def test_a_secret_in_the_body_is_redacted_before_it_is_stored(self):
        secret = 'sk-' + ('a' * 24)
        redacted = redact_secret_reply('密钥 ' + secret)
        self.assertNotIn(secret, redacted)
        self.assertIn('已拦截', redacted)

    def test_an_ordinary_body_is_left_alone(self):
        self.assertEqual(redact_secret_reply('37×19=703。'), '37×19=703。')


if __name__ == '__main__':
    unittest.main()
