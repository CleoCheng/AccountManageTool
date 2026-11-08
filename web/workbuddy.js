const workbuddy = {
  selected: new Set(),
  visible: [],
  queue: { items: [], current: null, round_no: 0, total_rounds: 0, round_items: [], completed: {}, node_quotas: [] },
  busy: false,
  autoRunning: false,
  modalOpen: false,
  code: "",
  codesByTask: {},
  focusedTaskId: null,
  selectedProxyUrl: "",
  selectedProxyNode: "",
  quotaData: null,
  _pausedTimer: null,
};

const workbuddySteps = {
  github: {
    title: "GitHub 注册",
    help: "先打开 GitHub 首页，随机走「中间邮箱框去注册」或「右上角 Sign up」通道进入注册页，再间隔 1~2 秒依次点击粘贴邮箱、密码、用户名。提交注册后确认此步。",
    confirm: "我已提交注册或确认该员工已有 GitHub 账号",
    next: "确认，进入邮箱验证",
    open: "打开 GitHub 首页并去注册",
  },
  email: {
    title: "邮箱验证",
    help: "进入此步会自动登录 mail.ggb6.com 读取 GitHub 验证码，并模拟真人点击输入框粘贴验证码。核对验证登录成功后确认此步。",
    confirm: "我已确认邮箱验证通过，并登录了这位员工的 GitHub",
    next: "确认，进入 WorkBuddy",
    open: "打开 GitHub 工作窗口",
  },
  workbuddy: {
    title: "WorkBuddy 授权",
    help: "先打开 WorkBuddy 国际版首页，模拟真人点击首页登录/注册入口后选择 GitHub 登录，核对授权页面上的用户名并完成注册。",
    confirm: "我已使用当前员工的 GitHub 成功登录 WorkBuddy 国际版",
    next: "确认，检查地区信息",
    open: "打开 WorkBuddy 首页去登录",
  },
  region: {
    title: "完善信息与地区",
    help: "检查右上角是否有「完善信息」按钮。如有，点击完善信息并确认非国区地区后保存；若页面无需补充则选择无需确认。",
    confirm: "我已核对页面并完成所需的信息确认",
    next: "确认，进入网页版",
    open: "返回 WorkBuddy 页面",
  },
  greeting: {
    title: "发送招呼并保存凭据",
    help: "在在线 WorkBuddy 新对话中随机发送招呼（hello/hi/你好/你好呀），等候回复后确认完成。本轮 2 个账号全部完成后将统一关闭两个指纹浏览器、清理缓存、切换订阅节点并开启下一轮。",
    confirm: "我已发送招呼，并看到 WorkBuddy 的回复",
    next: "确认收到回复，完成此人",
    open: "打开在线 WorkBuddy",
  },
};

function getFocusedWorkBuddyTask(queueObj = workbuddy.queue) {
  const roundItems = queueObj?.round_items || [];
  const activeInRound = roundItems.filter(item => ["ready", "paused", "verified"].includes(item.status));
  if (workbuddy.focusedTaskId) {
    const matched = activeInRound.find(item => item.id === workbuddy.focusedTaskId);
    if (matched) return matched;
  }
  return activeInRound[0] || queueObj?.current || null;
}

function selectWorkBuddyRoundSlot(taskId) {
  if (workbuddy.busy) return;
  const roundItems = workbuddy.queue?.round_items || [];
  const target = roundItems.find(item => item.id === taskId && ["ready", "paused", "verified"].includes(item.status));
  if (!target) return;
  workbuddy.focusedTaskId = target.id;
  workbuddy.code = workbuddy.codesByTask[target.id] || "";
  renderWorkBuddyModal();
}

function isWorkBuddyCompleted(item) {
  if (!item) return false;
  if (item.account_type === "域名邮箱") {
    return Boolean(
      workbuddy.queue.completed?.[item.account.toLowerCase()] ||
      item.custom_fields?.wb_reg_status === "已完成"
    );
  }
  return Boolean(item.custom_fields?.wb_credential?.accessToken);
}

function hasWorkBuddyCredential(item) {
  return Boolean(item?.custom_fields?.wb_credential?.accessToken);
}

function updateWorkBuddyQueue(queue) {
  if (!queue) return;
  const previous = workbuddy.queue;
  const lastId = queue.items.at(-1)?.id || 0;
  const previousLastId = previous.items.at(-1)?.id || 0;
  // Dashboard reads and action responses can arrive out of order.
  if (lastId < previousLastId || (lastId === previousLastId &&
      queue.items.some((item, i) => item.revision < (previous.items[i]?.revision || 0)))) return;
  const prevFocused = getFocusedWorkBuddyTask(previous);
  workbuddy.queue = queue;
  const nextFocused = getFocusedWorkBuddyTask(queue);
  workbuddy.focusedTaskId = nextFocused?.id || null;
  const prevRoundSig = (previous.round_items || []).map(i => `${i.id}:${i.revision}:${i.status}`).join("|");
  const nextRoundSig = (queue.round_items || []).map(i => `${i.id}:${i.revision}:${i.status}`).join("|");
  if (nextFocused?.id !== prevFocused?.id || nextFocused?.revision !== prevFocused?.revision || prevRoundSig !== nextRoundSig) {
    if (nextFocused?.id !== prevFocused?.id || nextFocused?.step !== prevFocused?.step) {
      if (prevFocused?.id) delete workbuddy.codesByTask[prevFocused.id];
    }
    workbuddy.code = nextFocused ? (workbuddy.codesByTask[nextFocused.id] || "") : "";
    renderWorkBuddyModal();
  }
}

function renderWorkBuddyCheckbox(item) {
  const allowDomain = item.account_type === "域名邮箱" && state.filterType === "域名邮箱";
  const allowWb = item.account_type === "WorkBuddy" && state.filterType === "WorkBuddy";
  if (!allowDomain && !allowWb) return "";
  return '<input type="checkbox" class="workbuddy-account-check" aria-label="选择邮箱 ' +
    escapeHtml(item.account) + '" ' + (workbuddy.selected.has(item.id) ? "checked " : "") +
    (workbuddy.queue.current || workbuddy.busy ? "disabled " : "") +
    'onchange="selectWorkBuddyAccount(' + item.id + ', this.checked)" />';
}

function renderWorkBuddyToolbar(visibleAccounts) {
  const toolbar = document.getElementById("workbuddy-toolbar");
  if (!toolbar) return;
  const domainView = state.filterType === "域名邮箱";
  const wbView = state.filterType === "WorkBuddy";
  toolbar.style.display = domainView || wbView || workbuddy.queue.current ? "flex" : "none";

  workbuddy.visible = domainView
    ? visibleAccounts.filter(item => item.account_type === "域名邮箱")
    : (wbView ? visibleAccounts.filter(item => item.account_type === "WorkBuddy") : []);

  const visibleIds = new Set(workbuddy.visible.map(item => item.id));
  workbuddy.selected = new Set([...workbuddy.selected].filter(id => visibleIds.has(id)));
  const count = workbuddy.selected.size;
  document.getElementById("workbuddy-selected-count").textContent =
    domainView ? ("已选 " + count + " 个邮箱") : ("已选 " + count + " 个账号");

  const all = document.getElementById("workbuddy-select-all");
  all.checked = count > 0 && count === visibleIds.size;
  all.indeterminate = count > 0 && count < visibleIds.size;
  all.disabled = !!workbuddy.queue.current || !visibleIds.size || workbuddy.busy;

  const btnPick2 = document.getElementById("workbuddy-pick-2");
  if (btnPick2) {
    btnPick2.style.display = domainView ? "" : "none";
    btnPick2.disabled = !!workbuddy.queue.current || workbuddy.busy;
  }
  const btnPick10 = document.getElementById("workbuddy-pick-10");
  if (btnPick10) {
    btnPick10.style.display = domainView ? "" : "none";
    btnPick10.disabled = !!workbuddy.queue.current || workbuddy.busy;
  }

  const selectedItems = workbuddy.visible.filter(item => workbuddy.selected.has(item.id));
  const pendingSelected = selectedItems.filter(item => item.account_type === "域名邮箱" && !isWorkBuddyCompleted(item));

  const start = document.getElementById("workbuddy-start");
  start.disabled = !pendingSelected.length || !!workbuddy.queue.current || workbuddy.busy;
  start.style.display = domainView ? "" : "none";

  const btnFile = document.getElementById("workbuddy-import-excel");
  if (btnFile) btnFile.style.display = domainView ? "" : "none";
  const btnFolder = document.getElementById("workbuddy-import-folder");
  if (btnFolder) btnFolder.style.display = domainView ? "" : "none";
  const btnQuota = document.getElementById("workbuddy-quota-btn");
  if (btnQuota) {
    btnQuota.style.display = domainView ? "" : "none";
    btnQuota.textContent = workbuddy.selectedProxyNode
      ? ("🌐 节点: " + workbuddy.selectedProxyNode)
      : "🌐 节点配额与解限";
  }

  const btnExport = document.getElementById("workbuddy-export-cred");
  if (btnExport) {
    btnExport.style.display = domainView || wbView ? "" : "none";
    const anyCredInView = workbuddy.visible.some(item => hasWorkBuddyCredential(item) || isWorkBuddyCompleted(item));
    btnExport.disabled = workbuddy.busy || (!count && !anyCredInView);
    btnExport.textContent = count ? ("📤 导出勾选凭据 (" + count + ")") : "📤 导出全部凭据 (JSON)";
  }

  const progress = document.getElementById("workbuddy-progress");
  progress.style.display = workbuddy.queue.items.length ? "" : "none";
  progress.textContent = workbuddy.queue.current ? "继续当前任务" : "查看上次结果";
}

function selectWorkBuddyAccount(id, selected) {
  if (selected) workbuddy.selected.add(id);
  else workbuddy.selected.delete(id);
  renderAccounts();
}

function selectAllWorkBuddy(selected) {
  workbuddy.selected = new Set(selected ? workbuddy.visible.map(item => item.id) : []);
  renderAccounts();
}

function quickSelectPendingWorkBuddy(limit = 2) {
  if (workbuddy.busy || workbuddy.queue.current) return;
  const candidates = workbuddy.visible.filter(
    item => item.account_type === "域名邮箱" && !isWorkBuddyCompleted(item)
  );
  // Prioritize in-progress accounts first so unfinished registrations finish first
  candidates.sort((a, b) => {
    const aProg = a.custom_fields?.wb_reg_status === "进行中" ? 0 : 1;
    const bProg = b.custom_fields?.wb_reg_status === "进行中" ? 0 : 1;
    return aProg - bProg;
  });
  const picked = candidates.slice(0, Math.max(1, Number(limit) || 2));
  workbuddy.selected = new Set(picked.map(item => item.id));
  renderAccounts();
  showToast(picked.length ? `已勾选 ${picked.length} 个待注册邮箱` : "当前列表没有待注册的域名邮箱");
}

async function startWorkBuddyQueue() {
  if (workbuddy.busy || !workbuddy.selected.size) return;
  const selectedItems = workbuddy.visible.filter(item => workbuddy.selected.has(item.id));
  const pendingIds = selectedItems
    .filter(item => item.account_type === "域名邮箱" && !isWorkBuddyCompleted(item))
    .map(item => item.id);
  if (!pendingIds.length) {
    showToast("所选邮箱均已完成 WorkBuddy 注册，无需重复注册");
    return;
  }
  const skippedCompleted = selectedItems.length - pendingIds.length;
  workbuddy.busy = true;
  renderAccounts();
  try {
    const result = await callApi(
      "start_workbuddy_queue",
      pendingIds,
      workbuddy.selectedProxyUrl || "",
      workbuddy.selectedProxyNode || ""
    );
    workbuddy.codesByTask = {};
    workbuddy.focusedTaskId = null;
    updateWorkBuddyQueue(result.queue);
    if (!result.ok) {
      showToast(result.error);
      return;
    }
    await loadDashboard();
    if (skippedCompleted > 0) {
      showToast("已自动跳过 " + skippedCompleted + " 个已完成邮箱，开始注册其余 " + pendingIds.length + " 个");
    }
    workbuddy.selected.clear();
    workbuddy.modalOpen = true;
    document.getElementById("workbuddy-modal").style.display = "flex";
    workbuddy.code = "";
    renderWorkBuddyModal();
    workbuddy.busy = false;
    setWorkBuddyBusy(false);
    await runWorkBuddyAutoLoop();
  } catch (_) {
    showToast("创建任务失败，请刷新任务进度后重试");
  } finally {
    workbuddy.busy = false;
    renderAccounts();
    setWorkBuddyBusy(false);
    schedulePausedWorkBuddyCheck();
  }
}

async function openWorkBuddyModal() {
  try {
    updateWorkBuddyQueue(await callApi("get_workbuddy_queue"));
    workbuddy.modalOpen = true;
    const focused = getFocusedWorkBuddyTask();
    workbuddy.code = focused ? (workbuddy.codesByTask[focused.id] || "") : "";
    document.getElementById("workbuddy-modal").style.display = "flex";
    renderWorkBuddyModal();
    schedulePausedWorkBuddyCheck();
  } catch (_) {
    showToast("读取任务进度失败，请稍后重试");
  }
}

function closeWorkBuddyModal() {
  if (workbuddy._pausedTimer) {
    clearTimeout(workbuddy._pausedTimer);
    workbuddy._pausedTimer = null;
  }
  workbuddy.autoRunning = false;
  workbuddy.modalOpen = false;
  workbuddy.code = "";
  document.getElementById("workbuddy-modal").style.display = "none";
  document.getElementById("workbuddy-body").innerHTML = "";
  loadDashboard();
}

function renderWorkBuddyModal() {
  if (!workbuddy.modalOpen) return;
  const task = getFocusedWorkBuddyTask();
  const items = workbuddy.queue.items;
  const roundItems = workbuddy.queue.round_items || [];
  const roundNo = workbuddy.queue.round_no || 1;
  const totalRounds = workbuddy.queue.total_rounds || 1;
  const statusNames = { ready: "待确认", paused: "等待人工验证", verified: "人工验证已确认", done: "已完成", skipped: "已跳过", cancelled: "已取消" };
  const history = '<details class="workbuddy-history"><summary>本批次总进度（共 ' + items.length +
    ' 人 · ' + totalRounds + ' 轮）</summary><ol>' + items.map(item => '<li><span>[第' + (item.round_no || 1) + '轮] ' + escapeHtml(item.email) +
    '</span><span>' + escapeHtml(item.username) + (item.proxy_node ? ' · 🌐' + escapeHtml(item.proxy_node) : '') +
    ' · ' + workbuddySteps[item.step].title + ' · ' +
    statusNames[item.status] + '</span></li>').join("") + '</ol></details>';
  const body = document.getElementById("workbuddy-body");
  if (!task) {
    const done = items.filter(item => item.status === "done").length;
    body.innerHTML = '<p class="workbuddy-finished">本批次已结束，完成 ' + done + ' 人，共 ' + items.length +
      ' 人。</p>' + history +
      '<div class="modal-footer">' +
      (done > 0 ? '<button class="btn btn-primary" onclick="closeWorkBuddyModal(); exportSelectedWorkBuddyJson();">导出 WorkBuddy2API 凭据 (JSON)</button>' : '') +
      '<button class="btn btn-secondary" onclick="closeWorkBuddyModal(); loadDashboard();">关闭</button></div>';
    return;
  }

  const roundNode = roundItems[0]?.proxy_node || task.proxy_node || "";
  const activeRoundCount = roundItems.filter(it => ["ready", "paused", "verified"].includes(it.status)).length;
  const roundSlotsHtml = roundItems.length
    ? '<div class="wb-round-banner">' +
      '<div class="wb-round-header">' +
        '<span>🔄 第 ' + roundNo + ' / ' + totalRounds + ' 轮（双指纹浏览器并排注册）</span>' +
        '<span>🌐 本轮节点：' + escapeHtml(roundNode || "直连/系统代理") + '</span>' +
        '<div style="display:flex;gap:6px;flex-wrap:wrap;">' +
        (activeRoundCount > 1
          ? '<button class="btn btn-secondary btn-xs" data-workbuddy-action onclick="workBuddyAction(\'open_round\')">🖥️ 同时打开/唤醒双窗口</button>'
          : '') +
        '<button class="btn btn-ghost btn-xs" data-workbuddy-action onclick="workBuddyAction(\'switch_node\')">🔀 换新节点重开本轮</button>' +
        '</div>' +
      '</div>' +
      '<div class="wb-dual-slots">' +
        roundItems.map((slotItem, idx) => {
          const isCurrent = slotItem.id === task.id;
          const clickable = ["ready", "paused", "verified"].includes(slotItem.status);
          const slotLabel = idx === 0 ? "指纹浏览器 ① (左窗)" : "指纹浏览器 ② (右窗)";
          const stateBadge = slotItem.status === "done"
            ? "✅ 已完成(待同轮结束统一关窗)"
            : (slotItem.status === "skipped"
                ? "⏭️ 已跳过"
                : (slotItem.status === "paused"
                    ? "⏸️ 等待人工验证"
                    : (workbuddySteps[slotItem.step]?.title || slotItem.step)));
          return '<button type="button" class="wb-slot-card' + (isCurrent ? ' active' : '') + (clickable ? '' : ' is-finished') + '" ' +
            (clickable ? ('onclick="selectWorkBuddyRoundSlot(' + slotItem.id + ')"') : 'disabled') + '>' +
            '<div class="wb-slot-top"><strong>🖥️ ' + slotLabel + '</strong><span>' + escapeHtml(stateBadge) + '</span></div>' +
            '<div class="wb-slot-email">' + escapeHtml(slotItem.email) + '</div>' +
            '</button>';
        }).join("") +
      '</div></div>'
    : "";

  const phase = workbuddySteps[task.step];
  const currentIndex = items.findIndex(item => item.id === task.id) + 1;
  const steps = '<ol class="workbuddy-steps">' + Object.entries(workbuddySteps).map(([key, step]) =>
    '<li class="' + (key === task.step ? "current" : "") + '">' + step.title + '</li>').join("") + '</ol>';

  const credBanner = ["workbuddy", "region", "greeting"].includes(task.step)
    ? '<div class="workbuddy-cred-bar">' +
      (task.has_credential
        ? '<span class="wb-cred-status ok">🔑 已捕获 WorkBuddy2API 登录凭据' + (task.has_refresh_token ? " (含 RefreshToken)" : "") + '</span>' +
          '<button class="copy-btn" data-workbuddy-action onclick="workBuddyAction(\'oauth\')">重新授权凭据</button>'
        : '<span class="wb-cred-status pending">🔑 登录凭据：确认完成时将自动获取，也可点击右侧立即授权捕获</span>' +
          '<button class="btn btn-secondary btn-xs" data-workbuddy-action onclick="workBuddyAction(\'oauth\')">获取登录凭据(JSON)</button>') +
      '</div>'
    : "";

  let fields = "";
  if (task.step === "github") {
    fields = '<label class="workbuddy-label" for="workbuddy-username">实际使用的 GitHub 用户名</label>' +
      '<input class="settings-input" id="workbuddy-username" maxlength="39" value="' + escapeHtml(task.username) + '" />' +
      '<p class="field-hint">默认使用邮箱用户名加随机四位数字。若用户名已占用，在注册页和这里同步修改。</p>';
  }
  if (task.step === "email") {
    fields = '<div class="workbuddy-actions"><button class="btn btn-secondary" data-workbuddy-action onclick="workBuddyAction(\'code\')">读取 GitHub 验证码</button>' +
      '<button class="btn btn-ghost" data-workbuddy-action onclick="workBuddyAction(\'mailbox\')">打开网页邮箱</button></div>' +
      '<div class="workbuddy-code" id="workbuddy-code-output" aria-live="polite"></div>';
  }
  if (task.step === "region") {
    fields = '<div class="workbuddy-region">' +
      '<label><input type="radio" name="workbuddy-region" value="confirmed" /> 已在页面确认实际地区</label>' +
      '<label><input type="radio" name="workbuddy-region" value="not_required" /> 页面无需确认</label></div>';
  }
  if (task.step === "greeting") {
    fields = '<div class="workbuddy-greeting"><strong>' + escapeHtml(task.greeting) +
      '</strong>' +
      (task.greeting_attempted ? '<span>已请求发送，请在对话中核对并等候回复。</span>' :
        '<button class="btn btn-secondary" data-workbuddy-action onclick="workBuddyAction(\'send\')">发送招呼</button>') +
      '<button class="copy-btn" data-workbuddy-action onclick="copyWorkBuddyField(\'greeting\')">复制招呼</button></div>';
  }
  const paused = task.status === "paused";
  const nodeBadge = task.proxy_node
    ? '<span>🌐 节点 · ' + escapeHtml(task.proxy_node) + '</span>'
    : '<span>🌐 直连/系统代理</span>';
  const fpBadge = task.fp_summary
    ? '<span>🧬 临时新指纹 · ' + escapeHtml(task.fp_summary) + '</span>'
    : '';

  const skipToWbBtn = ["github", "email"].includes(task.step)
    ? '<button class="btn btn-secondary" data-workbuddy-action onclick="skipToWorkBuddyStep()" title="若该邮箱之前已注册成功 GitHub，可跳过 GitHub 注册直接登录 WorkBuddy">⏭️ 已注册好 GitHub？直接进入 WorkBuddy</button>'
    : '';

  body.innerHTML = roundSlotsHtml +
    '<div class="workbuddy-person"><span>第 ' + currentIndex + ' / ' + items.length + ' 人</span><strong>' +
    escapeHtml(task.email) + '</strong><span>GitHub · ' + escapeHtml(task.username) + '</span>' +
    nodeBadge + fpBadge + '</div>' + steps +
    '<p class="workbuddy-help">' + phase.help + '</p>' +
    '<div id="workbuddy-message" role="status"></div>' +
    (paused ? '<div class="workbuddy-paused">⏸️ 已暂停等待人工完成验证：若浏览器中弹出滑块/拼图人机验证，请在浏览器窗口内拖动完成后点击下方「✅ 验证已完成，继续全自动注册」；若页面显示「访问暂时受限」，请直接点击「🔀 当前IP受限？拉黑并换新节点重开」自动切换干净节点！</div>' +
    '<div class="workbuddy-actions"><button class="btn btn-secondary" data-workbuddy-action onclick="workBuddyAction(\'show\')">打开待验证窗口</button>' +
    '<button class="btn btn-secondary" data-workbuddy-action onclick="workBuddyAction(\'switch_node\')">🔀 当前IP受限？拉黑并换新节点重开</button>' +
    skipToWbBtn + '</div>' :
    '<div class="workbuddy-actions"><button class="btn btn-secondary" data-workbuddy-action onclick="workBuddyAction(\'open\')">' +
    phase.open + '</button><button class="copy-btn" data-workbuddy-action onclick="copyWorkBuddyField(\'email\')">复制邮箱</button>' +
    '<button class="copy-btn" data-workbuddy-action onclick="copyWorkBuddyField(\'username\')">复制用户名</button>' +
    (["github", "email", "workbuddy"].includes(task.step) ? '<button class="copy-btn" data-workbuddy-action onclick="workBuddyAction(\'password\')">复制邮箱密码</button>' : "") +
    skipToWbBtn +
    '</div>' + fields + credBanner +
    '<label class="workbuddy-ack"><input id="workbuddy-ack" type="checkbox" onchange="updateWorkBuddyConfirm()" />' +
    phase.confirm + '</label>') +
    '<div class="workbuddy-actions workbuddy-controls">' +
    (paused ? '<button class="btn btn-primary" data-workbuddy-action onclick="resumeAndAutoRunWorkBuddy()">✅ 验证已完成，继续全自动注册</button>' +
    '<button class="btn btn-secondary" data-workbuddy-action onclick="workBuddyAction(\'resume\')">仅恢复为手动确认</button>' :
    '<button class="btn btn-primary" data-workbuddy-action onclick="resumeAndAutoRunWorkBuddy()">⚡ 继续全自动注册此轮</button>' +
    '<button class="btn btn-primary" data-workbuddy-action id="workbuddy-confirm" onclick="confirmWorkBuddyStep()" disabled>' + phase.next +
    '</button><button class="btn btn-secondary" data-workbuddy-action onclick="workBuddyAction(\'pause\')">遇到人机验证，暂停</button>') +
    '<button class="btn btn-ghost" data-workbuddy-action onclick="workBuddyAction(\'skip\')">跳过此人</button>' +
    '<button class="btn btn-ghost" data-workbuddy-action onclick="workBuddyAction(\'cancel\')">取消剩余任务</button></div>' +
    '<p class="field-hint">每次点击输入框、粘贴文本均自动间隔 1~2 秒模拟真人操作；每轮 2 个账号全部完成后统一关闭双浏览器、清理缓存并切换下一节点。</p>' + history;
  renderWorkBuddyCode();
  setWorkBuddyBusy(workbuddy.busy);
}

function updateWorkBuddyConfirm() {
  const button = document.getElementById("workbuddy-confirm");
  const isBusy = workbuddy.busy || workbuddy.autoRunning;
  if (button) button.disabled = isBusy || !document.getElementById("workbuddy-ack")?.checked;
}

function setWorkBuddyBusy(busy) {
  const isBusy = busy || workbuddy.autoRunning;
  document.querySelectorAll("[data-workbuddy-action]").forEach(button => { button.disabled = isBusy; });
  updateWorkBuddyConfirm();
  const body = document.getElementById("workbuddy-body");
  if (body) body.setAttribute("aria-busy", String(isBusy));
}

function syncLocalAccountsFromQueue(queueObj) {
  if (!queueObj || !Array.isArray(queueObj.items)) return;
  const stepLabels = {
    github: "GitHub注册",
    email: "邮箱验证",
    workbuddy: "WorkBuddy授权",
    region: "完善地区",
    greeting: "发送招呼",
  };
  for (const it of queueObj.items) {
    const acc = (state.accounts || []).find(a => a.id === it.account_id);
    if (acc && acc.account_type === "域名邮箱") {
      acc.custom_fields = acc.custom_fields || {};
      if (it.status === "done") {
        acc.custom_fields.wb_reg_status = "已完成";
        delete acc.custom_fields.wb_reg_step;
        delete acc.custom_fields.wb_reg_step_label;
      } else if (it.status !== "cancelled" && it.status !== "skipped") {
        acc.custom_fields.wb_reg_status = "进行中";
        acc.custom_fields.wb_reg_step = it.step;
        acc.custom_fields.wb_reg_step_label = stepLabels[it.step] || it.step;
      }
    }
  }
}

async function runWorkBuddyAutoLoop(skipInitialOpen = false) {
  if (workbuddy._pausedTimer) {
    clearTimeout(workbuddy._pausedTimer);
    workbuddy._pausedTimer = null;
  }
  if (workbuddy.autoRunning) return;
  workbuddy.autoRunning = true;
  setWorkBuddyBusy(true);
  const blockedTaskIds = new Set();
  let nodeRotations = 0;
  try {
    if (!skipInitialOpen) {
      await prepareCurrentWorkBuddyRound();
    }
    while (workbuddy.modalOpen && workbuddy.autoRunning) {
      const roundItems = workbuddy.queue?.round_items || [];
      const targetTask = roundItems.find(
        item => ["ready", "verified"].includes(item.status) && !blockedTaskIds.has(item.id)
      );
      if (!targetTask) {
        const pausedTask = roundItems.find(
          item => item.status === "paused" || blockedTaskIds.has(item.id)
        );
        if (pausedTask) {
          workbuddy.focusedTaskId = pausedTask.id;
          renderWorkBuddyModal();
        }
        break;
      }
      workbuddy.focusedTaskId = targetTask.id;
      renderWorkBuddyModal();

      const res = await requestWorkBuddyAction(targetTask, "auto_step");
      syncLocalAccountsFromQueue(res.queue || workbuddy.queue);
      renderAccounts();

      if (res.task_done) {
        await loadDashboard();
      }

      if (res.ok && res.round_transitioned) {
        if (res.switched_node) {
          if (nodeRotations >= 12) {
            blockedTaskIds.add(targetTask.id);
            continue;
          }
          nodeRotations++;
        }
        blockedTaskIds.clear();
        await prepareCurrentWorkBuddyRound();
        continue;
      }

      if (!res.ok || res.paused_for_human || !res.auto_advanced) {
        blockedTaskIds.add(targetTask.id);
        continue;
      }

      if (!workbuddy.queue?.current) {
        await loadDashboard();
        break;
      }
    }
  } catch (_) {
    showToast("自动注册流程遇到异常，已暂停");
  } finally {
    workbuddy.autoRunning = false;
    setWorkBuddyBusy(false);
    renderWorkBuddyModal();
    renderAccounts();
    schedulePausedWorkBuddyCheck();
  }
}

async function resumeAndAutoRunWorkBuddy() {
  const task = getFocusedWorkBuddyTask();
  if (!task || workbuddy.busy || workbuddy.autoRunning) return;
  const wasPaused = task.status === "paused";
  let rotatedDuringResume = false;
  if (wasPaused) {
    workbuddy.busy = true;
    setWorkBuddyBusy(true);
    try {
      const res = await requestWorkBuddyAction(task, "resume");
      if (res && res.ok && res.round_transitioned) {
        rotatedDuringResume = true;
      }
    } finally {
      workbuddy.busy = false;
      setWorkBuddyBusy(false);
    }
  }
  await runWorkBuddyAutoLoop(wasPaused && !rotatedDuringResume);
  schedulePausedWorkBuddyCheck();
}

async function skipToWorkBuddyStep() {
  const task = getFocusedWorkBuddyTask();
  if (!task || workbuddy.busy || workbuddy.autoRunning) return;
  const usernameInput = document.getElementById("workbuddy-username");
  const username = usernameInput ? usernameInput.value.trim() : (task.username || "");
  workbuddy.busy = true;
  setWorkBuddyBusy(true);
  try {
    const res = await requestWorkBuddyAction(task, "skip_to_workbuddy", username);
    if (res && res.ok) {
      syncLocalAccountsFromQueue(res.queue || workbuddy.queue);
      renderAccounts();
    }
  } finally {
    workbuddy.busy = false;
    setWorkBuddyBusy(false);
  }
  await runWorkBuddyAutoLoop(true);
  schedulePausedWorkBuddyCheck();
}

function schedulePausedWorkBuddyCheck() {
  if (workbuddy._pausedTimer) {
    clearTimeout(workbuddy._pausedTimer);
    workbuddy._pausedTimer = null;
  }
  if (!workbuddy.modalOpen || workbuddy.autoRunning || workbuddy.busy) return;
  const roundItems = workbuddy.queue?.round_items || [];
  const pausedTask = roundItems.find(item => item.status === "paused");
  if (!pausedTask) return;
  workbuddy._pausedTimer = setTimeout(async () => {
    workbuddy._pausedTimer = null;
    if (!workbuddy.modalOpen || workbuddy.autoRunning || workbuddy.busy) return;
    const curPaused = (workbuddy.queue?.round_items || []).find(item => item.status === "paused");
    if (!curPaused) return;
    try {
      const res = await callApi("workbuddy_action", curPaused.id, curPaused.revision, "check_paused", "");
      if (res?.queue) updateWorkBuddyQueue(res.queue);
      if (res?.ok && res.round_transitioned && res.switched_node) {
        if (res.message) showToast(res.message);
        await runWorkBuddyAutoLoop(false);
        return;
      }
      if (res?.ok && res.paused_resolved) {
        if (res.message) showToast(res.message);
        await runWorkBuddyAutoLoop(true);
        return;
      }
    } catch (_) {}
    schedulePausedWorkBuddyCheck();
  }, 2200);
}

async function confirmWorkBuddyStep() {
  if (!document.getElementById("workbuddy-ack")?.checked) return;
  const focused = getFocusedWorkBuddyTask();
  const step = focused?.step;
  let value = "";
  if (step === "github") value = document.getElementById("workbuddy-username").value.trim();
  if (step === "region") {
    value = document.querySelector('input[name="workbuddy-region"]:checked')?.value;
    if (!value) {
      showToast("请先选择地区确认结果");
      return;
    }
  }
  workBuddyAction("confirm", value);
}

async function workBuddyAction(action, value = "") {
  const task = getFocusedWorkBuddyTask();
  if (!task || workbuddy.busy) return;
  if (action === "cancel" && !confirm("取消本批次剩余任务并关闭两个工作窗口？已保存的步骤将保留。")) return;
  if (action === "skip" && !confirm("跳过这位员工？稍后可重新勾选继续。")) return;
  if (action === "switch_node" && !confirm("拉黑当前受限节点、清理浏览器缓存，并自动切换下一个干净订阅节点重新打开本轮窗口？")) return;
  const wasGreetingConfirm = action === "confirm" && task.step === "greeting";
  const prevTaskId = task.id;
  workbuddy.busy = true;
  setWorkBuddyBusy(true);
  try {
    const result = await requestWorkBuddyAction(task, action, value);
    if (result.ok && wasGreetingConfirm) {
      await loadDashboard();
    } else if (result.ok && (action === "confirm" || action === "auto_step")) {
      syncLocalAccountsFromQueue(result.queue);
      renderAccounts();
    }
    if (result.ok && (action === "confirm" || action === "skip" || action === "switch_node")) {
      if (result.round_transitioned) {
        await prepareCurrentWorkBuddyRound();
      } else {
        const nextTask = getFocusedWorkBuddyTask();
        if (nextTask && nextTask.id === prevTaskId) {
          await prepareCurrentWorkBuddyStep(nextTask);
        } else if (nextTask && nextTask.step === "email") {
          await prepareCurrentWorkBuddyStep(nextTask);
        }
      }
    }
  } catch (_) {
    const output = document.getElementById("workbuddy-message");
    if (output) output.textContent = "操作未完成，请重新打开任务进度后重试";
    showToast("操作未完成，请重新打开任务进度后重试");
  } finally {
    workbuddy.busy = false;
    setWorkBuddyBusy(false);
    renderAccounts();
    schedulePausedWorkBuddyCheck();
  }
}

async function prepareCurrentWorkBuddyRound() {
  for (let attempt = 0; attempt < 12; attempt++) {
    const task = getFocusedWorkBuddyTask();
    if (!workbuddy.modalOpen || !task) return;
    const roundReady = (workbuddy.queue.round_items || []).some(it => it.status === "ready");
    if (!roundReady) return;
    const res = await requestWorkBuddyAction(task, "open_round");
    if (res && res.ok && res.round_transitioned && res.switched_node) {
      continue;
    }
    return;
  }
}

async function prepareCurrentWorkBuddyStep(targetTask = null) {
  const task = targetTask || getFocusedWorkBuddyTask();
  if (!workbuddy.modalOpen || task?.status !== "ready") return;
  await requestWorkBuddyAction(task, task.step === "email" ? "code" : "open");
}

async function requestWorkBuddyAction(task, action, value = "") {
  const status = document.getElementById("workbuddy-message");
  if (status) {
    status.textContent = action === "code"
      ? "正在读取当前员工的验证邮件并模拟真人点击粘贴…"
      : (action === "auto_step"
          ? "正在全自动执行注册流水线步骤…"
          : (action === "open_round"
              ? "正在开启本轮双指纹浏览器，并按真人节奏（间隔1~2秒点击+粘贴）填写信息…"
              : (action === "oauth" ? "正在捕获 WorkBuddy2API 登录凭据…" : "正在模拟真人节奏处理（间隔1~2秒），请稍候…")));
  }
  if (action === "code") {
    delete workbuddy.codesByTask[task.id];
    workbuddy.code = "";
    renderWorkBuddyCode();
  }
  const result = await callApi("workbuddy_action", task.id, task.revision, action, value);
  updateWorkBuddyQueue(result.queue);
  const focused = getFocusedWorkBuddyTask();
  const sameStep = task.id === focused?.id && task.step === focused?.step;
  let message = result.error || result.message || "进度已保存";
  if (result.ok && result.code && workbuddy.modalOpen && sameStep) {
    workbuddy.codesByTask[task.id] = result.code;
    workbuddy.code = result.code;
    renderWorkBuddyCode();
    message = result.filled
      ? "已读取验证码并按真人点击+粘贴节奏填入页面，请核对后提交确认"
      : "已读取近期验证邮件，请核对并复制验证码";
  }
  if (result.ok && result.password && workbuddy.modalOpen && sameStep) {
    message = await writeClipboard(result.password) ? "邮箱密码已复制" : "复制失败，请在邮箱列表中查看密码";
  }
  const output = document.getElementById("workbuddy-message");
  if (output) output.textContent = message;
  if (!workbuddy.modalOpen || !result.ok || result.credential_saved || result.round_transitioned) showToast(message);
  return result;
}

function renderWorkBuddyCode() {
  const output = document.getElementById("workbuddy-code-output");
  if (output) output.innerHTML = workbuddy.code ? '<strong>' + escapeHtml(workbuddy.code) +
    '</strong><button class="copy-btn" data-workbuddy-action onclick="copyWorkBuddyField(\'code\')">复制验证码</button>' : "";
}

async function copyWorkBuddyField(field) {
  const task = getFocusedWorkBuddyTask();
  const value = field === "code" ? workbuddy.code : (field === "username" ? document.getElementById("workbuddy-username")?.value || task?.username : task?.[field]);
  if (value) {
    try {
      await writeClipboard(value);
      showToast("已复制");
    } catch (_) {
      showToast("复制失败，请手动选择文本复制");
    }
  }
}

// ============================================================
// EXCEL / CSV IMPORT & ACCOUNTDATA 1-CLICK IMPORT
// ============================================================
function triggerDomainEmailFileSelect() {
  const inp = document.getElementById("domain-email-file-input");
  if (inp) {
    inp.value = "";
    inp.click();
  }
}

async function onDomainEmailFileSelected(inputEl) {
  const file = inputEl?.files?.[0];
  if (!file) return;
  showToast("正在解析并导入 " + file.name + " …");
  try {
    const base64Data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("读取文件失败"));
      reader.readAsDataURL(file);
    });
    const res = await callApi("import_domain_email_file", base64Data, file.name);
    if (res && res.ok) {
      if (typeof closeAccountModal === "function") closeAccountModal();
      showToast(`✓ 成功导入 ${res.imported} 条域名邮箱（跳过已存在 ${res.skipped || 0} 条），默认标记未完成注册`);
      if (state.filterType !== "域名邮箱") {
        setFilterType("域名邮箱");
      } else {
        await loadDashboard();
      }
    } else {
      showToast(res?.error || "导入表格失败");
    }
  } catch (err) {
    showToast("导入异常: " + err.message);
  }
}

async function importAccountDataFolder() {
  showToast("正在扫描 accountData 目录下的 Excel/CSV 文件…");
  try {
    const res = await callApi("import_account_data_files");
    if (res && res.ok) {
      const filesDesc = (res.files || []).join(", ");
      showToast(`✓ 已从 [${filesDesc}] 导入 ${res.imported} 条邮箱（跳过重复 ${res.skipped || 0} 条）`);
      if (state.filterType !== "域名邮箱") {
        setFilterType("域名邮箱");
      } else {
        await loadDashboard();
      }
    } else {
      showToast(res?.error || "accountData 目录导入失败");
    }
  } catch (err) {
    showToast("导入 accountData 失败: " + err.message);
  }
}

// ============================================================
// SUBSCRIPTION NODE QUOTA & UNLOCK MODAL (每订阅节点限注册 2 个)
// ============================================================
async function openWorkBuddyQuotaModal() {
  const modal = document.getElementById("workbuddy-quota-modal");
  if (!modal) return;
  modal.style.display = "flex";
  const body = document.getElementById("workbuddy-quota-body");
  body.innerHTML = '<p style="color:var(--text-secondary);padding:16px 0;">正在读取本机订阅节点与配额使用情况…</p>';
  try {
    workbuddy.quotaData = await callApi("get_workbuddy_node_quotas");
    renderWorkBuddyQuotaModal();
  } catch (err) {
    body.innerHTML = '<p style="color:#f87171;padding:16px 0;">读取节点配额失败: ' + escapeHtml(err.message) + '</p>';
  }
}

function closeWorkBuddyQuotaModal() {
  const modal = document.getElementById("workbuddy-quota-modal");
  if (modal) modal.style.display = "none";
}

function renderWorkBuddyQuotaModal() {
  const body = document.getElementById("workbuddy-quota-body");
  if (!body || !workbuddy.quotaData) return;
  const data = workbuddy.quotaData;
  const nodes = data.nodes || [];
  const defaultPort = data.default_proxy_url || "";
  const isAuto = !workbuddy.selectedProxyNode;

  const modeHtml = `
    <div class="wb-quota-mode-bar">
      <div class="wb-quota-mode-option ${isAuto ? "active" : ""}" onclick="chooseWorkBuddyBatchNode('', '${escapeHtml(defaultPort)}')">
        <div>
          <strong>⚡ 自动轮换绑定订阅节点（推荐）</strong>
          <p>批量启动时自动分配可用订阅节点，每个节点最多注册 2 个 GitHub + WorkBuddy 账号，满额自动切换下一节点。</p>
        </div>
        <span class="wb-mode-check">${isAuto ? "✓ 当前生效" : "点击启用"}</span>
      </div>
    </div>
  `;

  if (!nodes.length) {
    body.innerHTML = modeHtml + `
      <div class="wb-quota-empty">
        当前未探测到 Clash / Mihomo 订阅节点列表（客户端: ${escapeHtml(data.client_name || "未在线")}）。
        启动代理软件后将自动列出全部订阅节点并执行每节点 2 账号配额保护。
      </div>
    `;
    return;
  }

  const rowsHtml = nodes.map(n => {
    const used = n.used_count || 0;
    const limit = n.quota_limit || 2;
    const unlocked = !!n.unlocked;
    const blocked = !!n.blocked;
    const exhausted = (blocked || used >= limit) && !unlocked;
    const isChosen = workbuddy.selectedProxyNode === n.node_name;
    const px = n.proxy_url || defaultPort;
    const statusBadge = unlocked
      ? `<span class="wb-quota-pill unlocked">🔓 已主动解限 (已用 ${used})</span>`
      : (blocked
          ? `<span class="wb-quota-pill exhausted">🚫 IP受限已拉黑 (禁止复用)</span>`
          : (exhausted
              ? `<span class="wb-quota-pill exhausted">🚫 已达上限 ${used}/${limit} (禁止复用)</span>`
              : `<span class="wb-quota-pill available">🟢 可用 ${used}/${limit}</span>`));

    return `
      <div class="wb-quota-row ${isChosen ? "selected" : ""} ${exhausted ? "is-exhausted" : ""}">
        <div class="wb-quota-info" onclick="chooseWorkBuddyBatchNode('${escapeHtml(n.node_name)}', '${escapeHtml(px)}')" title="点击指定使用此订阅节点">
          <strong>${isChosen ? "📌 " : ""}${escapeHtml(n.node_name)}</strong>
          <span class="wb-quota-meta">${escapeHtml(n.region || "")}${n.delay ? " · " + n.delay + "ms" : ""}</span>
        </div>
        <div class="wb-quota-actions">
          ${statusBadge}
          <button type="button" class="btn ${unlocked ? "btn-ghost" : "btn-secondary"} btn-xs"
            onclick="toggleWorkBuddyNodeUnlock('${escapeHtml(n.node_name)}', ${!unlocked}, '${escapeHtml(px)}')">
            ${unlocked ? "🔒 恢复限额" : "🔓 主动解限"}
          </button>
          ${(used > 0 || blocked) ? `<button type="button" class="btn btn-ghost btn-xs" onclick="resetWorkBuddyNodeQuota('${escapeHtml(n.node_name)}')">重置</button>` : ""}
        </div>
      </div>
    `;
  }).join("");

  body.innerHTML = modeHtml + `<div class="wb-quota-list">${rowsHtml}</div>`;
}

function chooseWorkBuddyBatchNode(nodeName, proxyUrl) {
  if (nodeName) {
    const found = (workbuddy.quotaData?.nodes || []).find(n => n.node_name === nodeName);
    if (found && found.exhausted && !found.unlocked) {
      showToast(`节点「${nodeName}」已达 2 个账号上限，请先点击右侧「主动解限」后再选用！`);
      return;
    }
  }
  workbuddy.selectedProxyNode = nodeName || "";
  workbuddy.selectedProxyUrl = proxyUrl || "";
  renderWorkBuddyQuotaModal();
  renderAccounts();
  showToast(nodeName ? `已指定本次注册绑定节点：${nodeName}` : "已切换为自动轮换绑定订阅节点");
}

async function toggleWorkBuddyNodeUnlock(nodeName, unlocked, proxyUrl) {
  try {
    workbuddy.quotaData = await callApi("set_workbuddy_node_unlock", nodeName, unlocked, proxyUrl || "");
    renderWorkBuddyQuotaModal();
    showToast(unlocked ? `🔓 已对节点「${nodeName}」主动解限，允许继续复用` : `🔒 已恢复节点「${nodeName}」2 账号限额`);
  } catch (err) {
    showToast("更新节点解限状态失败: " + err.message);
  }
}

async function resetWorkBuddyNodeQuota(nodeName) {
  if (!confirm(`确定重置节点「${nodeName}」的已注册计数吗？`)) return;
  try {
    workbuddy.quotaData = await callApi("reset_workbuddy_node_quota", nodeName);
    renderWorkBuddyQuotaModal();
    showToast(`✓ 已重置节点「${nodeName}」的配额计数`);
  } catch (err) {
    showToast("重置节点配额失败: " + err.message);
  }
}

// ============================================================
// EXPORT WORKBUDDY2API CREDENTIALS JSON (支持勾选导出)
// ============================================================
let lastExportedWorkBuddyJson = "";

async function exportSelectedWorkBuddyJson() {
  const selectedIds = [...workbuddy.selected];
  try {
    const res = await callApi("export_workbuddy_json", selectedIds.length ? selectedIds : null, true);
    if (!res || !res.ok) {
      showToast(res?.error || "所选账号尚未保存可导出的 WorkBuddy 登录凭据");
      return;
    }
    lastExportedWorkBuddyJson = res.json_text || "";
    const titleEl = document.getElementById("export-modal-title");
    const subEl = document.getElementById("export-modal-sub");
    const dlBtn = document.getElementById("btn-download-export-json");
    if (titleEl) titleEl.textContent = `导出 WorkBuddy2API 登录凭据 (${res.count} 个账号)`;
    if (subEl) {
      const savedHint = res.saved_path ? ` · 已同步写入 ${res.saved_path}` : "";
      const missHint = (res.missing_token_emails && res.missing_token_emails.length)
        ? `（另有 ${res.missing_token_emails.length} 个账号未捕获凭据已跳过）`
        : "";
      subEl.textContent = `标准 workbuddy-accounts JSON 格式，可直接导入 WorkBuddy2API${savedHint}${missHint}`;
    }
    if (dlBtn) dlBtn.style.display = "inline-flex";
    document.getElementById("export-textarea").value = lastExportedWorkBuddyJson;
    document.getElementById("export-modal").style.display = "flex";
  } catch (err) {
    showToast("导出凭据异常: " + err.message);
  }
}

function downloadExportJsonFile() {
  const text = document.getElementById("export-textarea")?.value || lastExportedWorkBuddyJson;
  if (!text) {
    showToast("暂无可下载的 JSON 内容");
    return;
  }
  const blob = new Blob([text], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  a.href = url;
  a.download = `workbuddy-accounts-${stamp}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  showToast("✓ 已触发下载 WorkBuddy2API 凭据 JSON 文件");
}

document.addEventListener("keydown", event => {
  if (event.key === "Escape") {
    if (workbuddy.modalOpen) closeWorkBuddyModal();
    closeWorkBuddyQuotaModal();
  }
});
