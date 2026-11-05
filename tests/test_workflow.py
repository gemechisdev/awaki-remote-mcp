from app.workflow import make_workflow

def test_workflow_has_dispatch_and_upload():
    text = make_workflow()
    assert "workflow_dispatch" in text
    assert "actions/upload-artifact@v4" in text
    assert "BUILD_COMMAND" in text
    assert "retention-days: 1" in text
