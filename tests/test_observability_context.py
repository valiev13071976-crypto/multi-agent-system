import unittest

from observability.context import ObservabilityContext


class ObservabilityContextTests(unittest.TestCase):
    def test_root_and_child_lineage(self):
        root = ObservabilityContext.root(workflow_id="wf-1", task_id="t-1")
        child = root.child()
        self.assertEqual(root.correlation_id, child.correlation_id)
        self.assertEqual(root.trace_id, child.trace_id)
        self.assertNotEqual(root.span_id, child.span_id)
        self.assertEqual(child.parent_span_id, root.span_id)
        self.assertTrue(root.started_at.tzinfo)
        with self.assertRaises(Exception):
            root.correlation_id = "x"  # frozen

    def test_with_workflow_task(self):
        root = ObservabilityContext.root()
        bound = root.with_workflow("wf").with_task("task")
        self.assertEqual(bound.workflow_id, "wf")
        self.assertEqual(bound.task_id, "task")
        self.assertEqual(bound.correlation_id, root.correlation_id)


if __name__ == "__main__":
    unittest.main()
