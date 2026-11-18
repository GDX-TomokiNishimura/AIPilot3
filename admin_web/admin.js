const app = document.querySelector("#app");
const statuses = {
  pending: "承認待ち", awaiting_reason: "理由確認待ち", approved: "承認済み", rejected: "却下",
};
const kinds = {
  business_knowledge: "業務知識", business_rule: "業務ルール", project: "プロジェクト情報",
  employee_guidance: "指導内容", employee_proficiency: "習熟度",
};
const actions = { submitted: "登録", revised: "修正", approved: "承認", rejected: "却下" };
const dates = new Intl.DateTimeFormat("ja-JP", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  hourCycle: "h23",
});
let view = 0;

function el(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children);
  return node;
}

function link(href, label, className = "") {
  const anchor = el("a", className, label);
  anchor.href = href;
  return anchor;
}

function button(label, className, click) {
  const node = el("button", className, label);
  node.type = "button";
  if (click) node.addEventListener("click", click);
  return node;
}

function statusBadge(status) {
  return el("span", `badge badge-${status}`, statuses[status]);
}

function time(value) {
  const node = el("time", "timestamp", dates.format(new Date(value)));
  node.dateTime = value;
  return node;
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "処理に失敗しました。");
  return data;
}

function heading(title, description, backLink) {
  const header = el("header", "page-heading");
  if (backLink) header.append(backLink);
  header.append(el("h1", "", title));
  if (description) header.append(el("p", "", description));
  return header;
}

function section(title, ...content) {
  return el("section", "panel", el("h2", "", title), ...content);
}

function filterControl(label, values, selected, update) {
  const select = el("select", "");
  for (const [value, name] of [["", "すべて"], ...Object.entries(values)]) {
    const option = el("option", "", name);
    option.value = value;
    select.append(option);
  }
  select.value = selected;
  select.addEventListener("change", () => update(select.value));
  return el("label", "filter", el("span", "", label), select);
}

function filterRoute(path, name, value) {
  return value ? `${path}?${name}=${encodeURIComponent(value)}` : path;
}

function criterionTable(items, complete, returnStatus = "") {
  if (!items.length) return el("p", "empty", "該当する判断基準はありません。");
  const labels = complete ? ["判断基準", "判断理由", "ステータス", "更新日時"] :
    ["判断基準", "ステータス", "更新日時"];
  const table = el("table", "");
  const header = el("tr", "");
  for (const label of labels) {
    const th = el("th", "", label);
    th.scope = "col";
    header.append(th);
  }
  const body = el("tbody", "");
  for (const item of items) {
    const href = filterRoute(`#/criteria/${item.criterion_id}`, "status", returnStatus);
    const row = el("tr", "", el("td", "criterion-cell", link(href, item.statement, "item-link")));
    if (complete) row.append(el("td", "reason-cell", item.reason || "—"));
    row.append(el("td", "status-cell", statusBadge(item.status)), el("td", "date-cell", time(item.updated_at)));
    body.append(row);
  }
  table.append(el("thead", "", header), body);
  return el("div", "table-scroll", table);
}

async function dashboard() {
  const { counts, recent } = await api("/api/dashboard");
  const grid = el("div", "stats");
  for (const status of ["pending", "awaiting_reason", "approved"]) {
    const value = el("strong", "stat-number", String(counts[status]), el("span", "", "件"));
    const card = link(`#/criteria?status=${status}`, "", `stat stat-${status}`);
    card.append(statusBadge(status), value, el("span", "stat-arrow", "→"));
    grid.append(card);
  }
  const recentTitle = el("div", "panel-title", el("h2", "", "最近更新された判断基準"),
    link("#/criteria", "判断基準をすべて見る →", "text-link"));
  const activity = el("section", "panel dashboard-activity", recentTitle, criterionTable(recent, false));
  return [heading("ダッシュボード", "判断基準のレビュー状況を確認できます。"), grid, activity];
}

async function criteriaList(params) {
  const requested = params.get("status");
  const status = Object.hasOwn(statuses, requested) ? requested : "";
  const { items } = await api(filterRoute("/api/criteria", "status", status));
  const toolbar = el("div", "panel-title",
    el("h2", "", "判断基準一覧"),
    filterControl("ステータス", statuses, status, (value) => {
      location.hash = filterRoute("#/criteria", "status", value);
    }),
  );
  return [heading("判断基準", "判断基準の内容と理由を確認し、レビューできます。"),
    el("section", "panel", toolbar, criterionTable(items, true, status))];
}

function reviewHistory(history) {
  const list = el("ol", "timeline");
  for (const event of [...history].reverse()) {
    const details = el("details", "history-details",
      el("summary", "", `第${event.revision}版の内容`),
      el("p", "long-text", event.statement),
      el("p", "long-text muted", event.reason || "理由は未入力です。"),
    );
    list.append(el("li", "",
      el("strong", "", actions[event.action]),
      el("span", "history-meta", event.actor, time(event.occurred_at)),
      details,
    ));
  }
  return list;
}

function criterionActions(criterion, returnStatus, currentView) {
  const notice = el("p", "form-error");
  notice.setAttribute("role", "alert");
  notice.hidden = true;
  const form = el("form", "edit-form");
  form.hidden = true;
  const statement = el("textarea", "");
  statement.value = criterion.statement;
  statement.rows = 3;
  statement.required = true;
  const reason = el("textarea", "");
  reason.value = criterion.reason ?? "";
  reason.rows = 4;
  const edit = button("修正する", "btn secondary", () => {
    form.hidden = false;
    edit.disabled = true;
    approve.disabled = true;
    reject.disabled = true;
    statement.focus();
  });
  const cancel = button("キャンセル", "btn secondary", () => {
    form.hidden = true;
    notice.hidden = true;
    statement.value = criterion.statement;
    reason.value = criterion.reason ?? "";
    edit.disabled = false;
    approve.disabled = !criterion.reason;
    reject.disabled = false;
    edit.focus();
  });
  const save = button("修正を保存", "btn primary");
  save.type = "submit";
  const approve = button("承認する", "btn primary", () => submit("approve", "判断基準を承認しました。"));
  const reject = button("却下する", "btn danger", () => submit("reject", "判断基準を却下しました。"));
  const pending = criterion.status === "pending" || criterion.status === "awaiting_reason";
  approve.disabled = !criterion.reason;
  const buttons = el("div", "actions", edit);
  if (pending) buttons.append(approve, reject);
  form.append(el("label", "", "判断基準", statement), el("label", "", "判断理由", reason),
    el("div", "actions", save, cancel));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit("revise", "修正を保存しました。", { statement: statement.value, reason: reason.value });
  });

  async function submit(action, message, fields = {}) {
    const controls = [...buttons.querySelectorAll("button"), save, cancel, statement, reason];
    const previous = controls.map((node) => node.disabled);
    for (const node of controls) node.disabled = true;
    notice.hidden = true;
    try {
      await api(`/api/criteria/${criterion.criterion_id}/${action}`,
        { expected_revision: criterion.revision, ...fields });
      if (currentView === view) await render(message);
    } catch (error) {
      notice.textContent = error.message;
      notice.hidden = false;
    } finally {
      controls.forEach((node, index) => { node.disabled = previous[index]; });
    }
  }
  const panel = section("レビュー操作", buttons, form, notice);
  if (criterion.status === "awaiting_reason") {
    panel.insertBefore(el("p", "hint", "承認するには判断理由を入力して保存してください。"), buttons);
  }
  return panel;
}

async function criterionDetail(id, params, currentView) {
  const { criterion, messages, history } = await api(`/api/criteria/${id}`);
  const status = params.get("status");
  const back = link(filterRoute("#/criteria", "status", Object.hasOwn(statuses, status) ? status : ""),
    "← 判断基準一覧", "back-link");
  const summary = el("section", "panel",
    el("div", "panel-title", el("h2", "", "判断基準"), statusBadge(criterion.status)),
    el("p", "statement long-text", criterion.statement),
    el("div", "reason-block", el("h3", "", "判断理由"),
      el("p", criterion.reason ? "long-text" : "muted", criterion.reason || "理由は未入力です。")),
  );
  const evidence = section("根拠メッセージ");
  if (!messages.length) evidence.append(el("p", "empty", "根拠メッセージは登録されていません。"));
  for (const message of messages) {
    evidence.append(el("article", "message",
      el("header", "", el("strong", "", message.sender_display_name || message.sender_id), time(message.sent_at)),
      el("p", "long-text", message.body),
    ));
  }
  return [heading("判断基準の詳細", null, back),
    el("div", "detail-grid", el("div", "stack", summary, evidence, criterionActions(criterion, status, currentView)),
      section("変更履歴", reviewHistory(history)))];
}

async function knowledgeList(params) {
  const requested = params.get("kind");
  const kind = Object.hasOwn(kinds, requested) ? requested : "";
  const { items } = await api(filterRoute("/api/knowledge", "kind", kind));
  const toolbar = el("div", "panel-title", el("h2", "", "業務知識一覧"),
    filterControl("カテゴリ", kinds, kind, (value) => {
      location.hash = filterRoute("#/knowledge", "kind", value);
    }),
  );
  const list = el("ul", "knowledge-list");
  for (const item of items) {
    const href = filterRoute(`#/knowledge/${item.id}`, "kind", kind);
    const anchor = link(href, "", "knowledge-link");
    anchor.append(el("span", "kind-badge", kinds[item.kind]), el("strong", "", item.title),
      el("span", "list-arrow", "→"));
    list.append(el("li", "", anchor));
  }
  return [heading("業務知識", "業務ルールやプロジェクト、社員への指導内容を参照できます。"),
    el("section", "panel", toolbar, items.length ? list : el("p", "empty", "該当する業務知識はありません。"))];
}

async function knowledgeDetail(id, params) {
  const entry = await api(`/api/knowledge/${id}`);
  const kind = params.get("kind");
  const back = link(filterRoute("#/knowledge", "kind", Object.hasOwn(kinds, kind) ? kind : ""),
    "← 業務知識一覧", "back-link");
  const panel = section("内容", el("span", "kind-badge", kinds[entry.kind]));
  if (entry.subject_ref) panel.append(el("p", "muted", `対象 ${entry.subject_ref}`));
  panel.append(el("p", "long-text knowledge-body", entry.body));
  return [heading(entry.title, null, back), panel];
}

async function render(message = "") {
  const current = ++view;
  const [path, query] = (location.hash.slice(1) || "/").split("?", 2);
  const params = new URLSearchParams(query);
  const detail = /^\/(criteria|knowledge)\/([1-9][0-9]{0,17})$/.exec(path);
  const active = path.split("/")[1] || "dashboard";
  for (const anchor of document.querySelectorAll("nav a")) {
    if (anchor.dataset.section === active) anchor.setAttribute("aria-current", "page");
    else anchor.removeAttribute("aria-current");
  }
  app.setAttribute("aria-busy", "true");
  const loading = el("p", "loading", "読み込み中…");
  loading.setAttribute("role", "status");
  app.replaceChildren(loading);
  try {
    let page;
    if (path === "/") page = await dashboard();
    else if (path === "/criteria") page = await criteriaList(params);
    else if (path === "/knowledge") page = await knowledgeList(params);
    else if (detail?.[1] === "criteria") page = await criterionDetail(detail[2], params, current);
    else if (detail?.[1] === "knowledge") page = await knowledgeDetail(detail[2], params);
    else throw new Error("ページが見つかりません。");

    if (current !== view) return;
    if (message) {
      const success = el("p", "success", message);
      success.setAttribute("role", "status");
      page.splice(1, 0, success);
    }
    app.replaceChildren(...page);
    document.title = `${page[0].querySelector("h1").textContent} | AIPilot3`;
    app.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  } catch (error) {
    if (current !== view) return;
    const notice = el("p", "form-error", error.message);
    notice.setAttribute("role", "alert");
    app.replaceChildren(heading("表示できませんでした"), notice,
      button("再読み込み", "btn secondary", () => render()));
  } finally {
    if (current === view) app.setAttribute("aria-busy", "false");
  }
}

window.addEventListener("hashchange", () => render());
render();
