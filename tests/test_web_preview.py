from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app


def test_web_shell_and_local_assets_are_served(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))

    page = client.get("/")
    css = client.get("/static/styles.css")
    script = client.get("/static/app.js")
    assert "需要 OCR · 暂无可读文本" not in script.text
    assert "仍有页面未识别完整" in script.text
    batch_script = client.get("/static/upload_batch.mjs")
    polish_css = client.get("/static/page_polish.css")
    polish_script = client.get("/static/page_polish.mjs")
    assert polish_css.status_code == 200 and polish_css.headers["cache-control"] == "no-store"
    assert polish_script.status_code == 200 and "materialSelection" in polish_script.text
    assert 'id="material-search"' in page.text
    assert 'id="material-topic"' in page.text
    assert 'id="material-no-results"' in page.text
    assert 'id="reader-source-info"' in page.text
    assert "提取正文 · 非原版排版" in page.text

    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "Project Watch" in page.text
    assert 'id="project-list"' in page.text
    assert "从一个项目名" in page.text
    assert 'id="impact-list"' in page.text
    assert 'id="impact-dialog"' in page.text
    assert 'id="start-public-search"' in page.text
    assert 'id="overview-public-monitoring"' in page.text
    assert "只有项目名称？" in page.text
    assert 'id="public-search-dialog"' in page.text
    assert "本机开发预览仅用合成材料测试" in page.text
    assert "真实项目资料请等待隔离环境验收" in page.text
    assert "真实资料暂勿拖入" in page.text
    assert 'id="local-material-status"' in page.text
    assert 'id="local-material-updates"' in page.text
    assert 'id="local-material-all"' in page.text
    assert 'id="metric-series-list"' in page.text
    assert 'id="financial-screen-list"' in page.text
    assert 'id="financial-screen-status"' in page.text
    assert 'id="monitoring-domains"' in page.text
    assert 'id="activity-timeline"' in page.text
    assert 'id="operating-metrics-list"' in page.text
    assert 'id="operating-metrics-status"' in page.text
    assert 'id="view-report"' in page.text
    assert 'id="upload-status"' in page.text
    assert 'id="upload-progress"' in page.text
    assert 'id="report-print"' in page.text
    assert 'id="metric-dialog"' in page.text
    assert '<option value="资本与持仓">资本与持仓</option>' in page.text
    assert 'id="metric-name"' in page.text
    assert 'id="review-open-source"' in page.text
    assert 'id="batch-review-toolbar"' in page.text
    assert 'id="batch-review-dialog"' in page.text
    assert 'id="batch-review-list"' in page.text
    assert page.text.index('id="local-material-updates"') < page.text.index(
        'id="public-search-starter"'
    )
    assert 'class="impact-dialog-body"' in page.text
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    assert css.headers["cache-control"] == "no-store"
    assert "#impact-dialog .dialog-actions{flex:none" in css.text
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert script.headers["cache-control"] == "no-store"
    assert batch_script.status_code == 200 and "javascript" in batch_script.headers["content-type"]
    assert "uploadBatch" in script.text
    assert "与同名旧材料比较" in script.text
    assert "同名不等于已核验版本关系" in script.text
    assert "openImpactReview" in script.text
    assert "名称匹配" in script.text
    assert "旧回执 · 未按名称筛选" in script.text
    assert "来源限流 · 请稍后再试" in script.text
    assert "function renderLocalMaterialUpdates()" in script.text
    assert '$("header-upload").textContent = "选择文件 →"' in script.text
    assert "documents.slice(-8)" in script.text
    assert "openImpactReview(impact)" in script.text
    assert "function openBatchReview()" in script.text
    assert "observations/batch-review`" in script.text
    assert "function renderMetricSeries()" in script.text
    assert "function openMetricDraft(source, fragment, suggestion = null)" in script.text
    assert "renderFinancialReviewDrafts(panel, note, mapping, source, fragments);" in script.text
    assert "待核对财务字段" in script.text
    assert "request(`/api/projects/${id}/metric-series`)" in script.text
    assert "request(`/api/projects/${id}/financial-screen`)" in script.text
    assert "function renderFinancialScreen()" in script.text
    assert "item.evidence || []" in script.text
    assert "function renderActivityTimeline()" in script.text
    assert "function renderOperatingMetrics()" in script.text
    assert "function renderReport(brief)" in script.text
    assert 'reportSection("跨期指标")' in script.text
    assert "request(`/api/projects/${id}/monitoring/domains`)" in script.text
    assert "gdelt.project_name" in script.text
    assert '$("overview-public-monitoring").addEventListener("click"' in script.text
    assert 'id="public-search-provider"' in page.text
    assert "确认发送并开启" in page.text
    assert "同名可能误报" not in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert "script-src 'self'" in page.headers["content-security-policy"]


def test_static_mount_cannot_expose_database(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))

    response = client.get("/static/../../watch.sqlite3")

    assert response.status_code == 404


def test_monitor_dashboard_assets_and_navigation_are_part_of_product(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    page = client.get("/").text
    assert 'id="project-directory-search"' in page
    assert 'id="close-directory"' in page
    assert 'data-view="materials" data-primary' in page
    assert 'id="internal-changes"' in page
    assert page.index("/static/dashboard.css") > page.index("/static/client.css")
    for path in ["dashboard.css", "monitor_dashboard.mjs", "dashboard_charts.mjs", "favicon.svg"]:
        result = client.get(f"/static/{path}")
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
    css = client.get("/static/dashboard.css").text
    assert "#view-overview > #monitor-dashboard:not([hidden])" in css
    shell = client.get("/static/client_shell.mjs").text
    assert "renderMonitorDashboard(state" in shell
    assert "$('view-business').querySelector('.section-intro').after($('company-internal'))" in shell
