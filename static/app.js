async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.json();
}

function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// 需要登录才能做的操作（保存等）：失败时明确提示
async function guardSave(r) {
  if (r && r.url) {
    window.location = r.url;
    return true;
  }
  alert((r && (r.message || r.feedback)) || "该操作需要登录后才能使用，请先登录");
  return false;
}

// ===== 输入草稿：自动暂存 / 离开后回填 / 提交或清空后删除 =====
// 存储策略：登录用户走服务端 per-user 草稿库（/api/draft），未登录或接口不可用自动降级到 localStorage
// 键名规则：场景.条目（如 roadmap.direction / practice.answer），不同场景互不覆盖
const DRAFT_LS_PREFIX = "study-draft:";
let draftStore = null;          // 服务端草稿快照（登录后）
const draftTimers = {};         // 防抖计时器
let draftFields = [];           // 本页所有 [data-draft] 输入框

function draftLSKey(key) {
  return DRAFT_LS_PREFIX + key;
}

function readLocalDraft(key) {
  try { return localStorage.getItem(draftLSKey(key)) || ""; } catch (e) { return ""; }
}

function writeLocalDraft(key, value) {
  try {
    if (value) localStorage.setItem(draftLSKey(key), value);
    else localStorage.removeItem(draftLSKey(key));
  } catch (e) {}
}

// 写入草稿：本地立即落盘（刷新/离线/未登录也能恢复），登录态再防抖同步服务端
function saveDraft(key, value) {
  writeLocalDraft(key, value);
  if (draftStore === null) return;          // 未登录或接口不可用：只用本地
  if (value) draftStore[key] = { value: value };
  else delete draftStore[key];
  clearTimeout(draftTimers[key]);
  draftTimers[key] = setTimeout(() => {
    fetch("/api/draft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: key, value: value }),
    }).catch(() => {});
  }, 400);
}

// 清除草稿：提交成功 / 用户手动清空或放弃
function clearDraft(key) {
  writeLocalDraft(key, "");
  if (draftStore !== null) delete draftStore[key];
  clearTimeout(draftTimers[key]);
  if (draftStore === null) return;
  fetch("/api/draft", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: key, value: "" }),
  }).catch(() => {});
}

// 回填：只覆盖"空的或上次由草稿填入的"内容，不打断用户已输入/服务端预填的值
function applyDraft(el, value) {
  if (!value) return;
  if (el.value !== "" && el.value !== el.__draftShown) return;
  el.value = value;
  el.__draftShown = value;
  showDraftHint(el);
}

// 草稿提示条：告知用户内容已恢复，并提供"放弃草稿"入口
function showDraftHint(el) {
  if (el.__hintEl) { el.__hintEl.style.display = "flex"; return; }
  const key = el.dataset.draft;
  const hint = document.createElement("div");
  hint.style.cssText = "display:flex; align-items:center; gap:8px; margin-top:6px; font-size:12px; color:var(--muted)";
  hint.innerHTML = '<span>🗒️ 已恢复上次未提交的内容</span>' +
    '<button type="button" class="btn ghost draft-discard" style="padding:2px 8px; font-size:12px">丢弃</button>';
  el.parentNode.insertBefore(hint, el.nextSibling);
  el.__hintEl = hint;
  hint.querySelector(".draft-discard").addEventListener("click", () => {
    clearDraft(key);
    el.value = "";
    el.__draftShown = "";
    hint.style.display = "none";
  });
}

// 初始化：先用本地缓存同步回填（无闪烁），再拉服务端草稿覆盖，最后绑定输入监听
function initDrafts() {
  draftFields = Array.from(document.querySelectorAll("[data-draft]"));
  if (!draftFields.length) return;

  draftFields.forEach((el) => {
    el.__draftShown = "";
    applyDraft(el, readLocalDraft(el.dataset.draft));
  });

  fetch("/api/drafts")
    .then((r) => (r.ok ? r.json() : null))
    .then((j) => {
      if (j && j.ok) {
        draftStore = j.drafts || {};
        draftFields.forEach((el) => {
          const d = draftStore[el.dataset.draft];
          if (d && d.value) {
            applyDraft(el, d.value);
            writeLocalDraft(el.dataset.draft, d.value);   // 本地也同步一份，离线可恢复
          }
        });
      } else {
        draftStore = null;    // 未登录等：降级为纯本地草稿
      }
    })
    .catch(() => { draftStore = null; });

  draftFields.forEach((el) => {
    const key = el.dataset.draft;
    let timer = null;
    el.addEventListener("input", () => {
      const v = el.value;
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (v.trim()) saveDraft(key, v);
        else clearDraft(key);        // 手动清空 = 放弃草稿，直接删除
      }, 300);
    });
    // 回车提交的路径（非 form 的场景）也能顺带清掉对应草稿
    el.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { clearDraft(key); el.value = ""; if (el.__hintEl) el.__hintEl.style.display = "none"; }
    });
  });
}

// 提交任务 → 轮询状态 → 完成后回调（用户可随时离开，任务继续后台跑）
async function submitAndPoll(url, body, onResult) {
  let submit;
  try {
    submit = await postJSON(url, body);
  } catch (e) {
    onResult({ message: "网络异常：" + e });
    return;
  }
  if (!submit || !submit.ok) {
    onResult({ message: (submit && (submit.message || submit.feedback)) || "提交失败" });
    return;
  }
  let retries = 0;
  const maxRetries = 6; // 任务级网络重试超过次数就放弃，让用户重试
  const poll = async () => {
    try {
      const r = await fetch("/api/jobs/" + submit.job_id);
      const j = await r.json();
      const job = j.job;
      if (!job) {
        if (++retries > maxRetries) { onResult({ message: "任务状态获取失败（已重试 " + maxRetries + " 次）" }); return; }
        setTimeout(poll, 2000);
        return;
      }
      if (job.status === "done") {
        onResult({ result: job.result || {} });
      } else if (job.status === "error") {
        onResult({ message: (job.result && job.result.message) || "任务失败" });
      } else {
        setTimeout(poll, 1500);
      }
    } catch (e) {
      if (++retries > maxRetries) { onResult({ message: "网络异常：" + e }); return; }
      setTimeout(poll, 2000);
    }
  };
  setTimeout(poll, 1500);
}

document.addEventListener("DOMContentLoaded", () => {
  initDrafts();   // 最先执行：把未提交的内容回填进输入框

  // ===== 问 AI =====
  const askForm = document.getElementById("ask-form");
  if (askForm) {
    const answerBox = document.getElementById("answer");
    askForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = document.getElementById("ask-input");
      const q = input.value.trim();
      if (!q) return;
      answerBox.style.display = "block";
      answerBox.textContent = "思考中…";
      const data = await postJSON("/api/ask", { question: q });
      if (data.answer) clearDraft("ask.question");
      answerBox.textContent = data.answer || "（出错了）";
    });
  }

  // ===== 搜索页「让 AI 总结」 =====
  const sumBtn = document.getElementById("summarize-btn");
  if (sumBtn) {
    sumBtn.addEventListener("click", async () => {
      const query = sumBtn.dataset.query;
      const results = JSON.parse(sumBtn.dataset.results || "[]");
      const box = document.getElementById("ai-summary");
      box.style.display = "block";
      box.textContent = "总结中…";
      const data = await postJSON("/api/summarize", { query, results });
      box.textContent = data.answer || "（出错了）";
    });
  }

  // ===== 学习路线 =====
  const roadmapForm = document.getElementById("roadmap-form");
  if (roadmapForm) {
    const resultBox = document.getElementById("roadmap-result");
    const saveBox = document.getElementById("roadmap-save");
    const dirInput = document.getElementById("direction");
    let lastRoadmap = "";

    async function generate(d) {
      resultBox.style.display = "block";
      resultBox.textContent = "正在后台生成路线图…你可以先去别的页面，结果会在这里和「任务」页出现。";
      saveBox.style.display = "none";
      await submitAndPoll("/api/roadmap", { direction: d }, (res) => {
        if (res.result && res.result.html) {
          lastRoadmap = res.result.roadmap;
          clearDraft("roadmap.direction");       // 生成成功 → 清掉草稿
          resultBox.innerHTML = res.result.html;
          saveBox.style.display = "block";
          saveBox.innerHTML = '<button class="btn" id="save-roadmap">保存为笔记</button>';
          document.getElementById("save-roadmap").addEventListener("click", async () => {
            const r = await postJSON("/api/roadmap/save", { title: d + " 学习路线", content: lastRoadmap });
            await guardSave(r);
          });
        } else {
          resultBox.textContent = res.message || "（生成失败）";
        }
      });
    }

    document.querySelectorAll(".chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        dirInput.value = chip.dataset.direction;
        generate(chip.dataset.direction);
      });
    });
    roadmapForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const d = dirInput.value.trim();
      if (!d) return;
      generate(d);
    });
  }

  // ===== 一键整理 =====
  const orgBtn = document.getElementById("organize-btn");
  if (orgBtn) {
    const orgBox = document.getElementById("organize-result");
    orgBtn.addEventListener("click", async () => {
      orgBox.style.display = "block";
      orgBox.textContent = "正在后台整理…（可先做别的，结果也会出现在「任务」页）";
      await submitAndPoll("/api/organize", {}, (res) => {
        orgBox.textContent = (res.result && res.result.message) || res.message || "（出错了）";
        if (res.result && res.result.message) setTimeout(() => location.reload(), 1500);
      });
    });
  }

  // ===== 首页：输入知识点 → 四段循环 =====
  const homeTopic = document.getElementById("home-topic");
  if (homeTopic) {
    document.getElementById("go-demo").addEventListener("click", () => {
      const t = homeTopic.value.trim();
      window.location = t ? "/search?q=" + encodeURIComponent(t + " 动画 演示 入门") : "/search";
    });
    document.getElementById("go-roadmap").addEventListener("click", () => {
      const t = homeTopic.value.trim();
      window.location = t ? "/roadmap?d=" + encodeURIComponent(t) : "/roadmap";
    });
    const goVideo = document.getElementById("go-video");
    if (goVideo) goVideo.addEventListener("click", () => {
      const t = homeTopic.value.trim();
      window.location = t ? "/search?q=" + encodeURIComponent(t + " 课程 讲解") : "/search";
    });
    const goDouyin = document.getElementById("go-douyin");
    if (goDouyin) goDouyin.addEventListener("click", () => {
      const t = homeTopic.value.trim() || "";
      window.open("https://www.douyin.com/search/" + encodeURIComponent(t), "_blank");
    });
    const goXhs = document.getElementById("go-xhs");
    if (goXhs) goXhs.addEventListener("click", () => {
      const t = homeTopic.value.trim() || "";
      window.open("https://www.xiaohongshu.com/search_result?keyword=" + encodeURIComponent(t), "_blank");
    });
    document.getElementById("go-practice").addEventListener("click", () => {
      const t = homeTopic.value.trim();
      window.location = t ? "/practice?topic=" + encodeURIComponent(t) : "/practice";
    });
    document.getElementById("go-extend").addEventListener("click", () => {
      const t = homeTopic.value.trim();
      window.location = t ? "/extend?topic=" + encodeURIComponent(t) : "/extend";
    });
    const goMap = document.getElementById("go-map");
    if (goMap) goMap.addEventListener("click", () => {
      const t = homeTopic.value.trim();
      window.location = t ? "/map?d=" + encodeURIComponent(t) : "/map";
    });
  }

  // ===== 练习页 =====
  const genBtn = document.getElementById("gen-btn");
  if (genBtn) {
    const topicInput = document.getElementById("topic");
    const resultBox = document.getElementById("practice-result");
    const findBox = document.getElementById("find-result");
    const checkPanel = document.getElementById("check-panel");
    const myAnswer = document.getElementById("my-answer");
    const checkResult = document.getElementById("check-result");
    const checkBtn = document.getElementById("check-btn");
    const findBtn = document.getElementById("find-btn");
    let difficulty = "进阶";
    let lastProblems = "";

    document.querySelectorAll(".chip[data-diff]").forEach((chip) => {
      chip.addEventListener("click", () => {
        document.querySelectorAll(".chip[data-diff]").forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
        difficulty = chip.dataset.diff;
      });
    });

    genBtn.addEventListener("click", async () => {
      const topic = topicInput.value.trim();
      if (!topic) { alert("请先输入知识点"); return; }
      resultBox.style.display = "block";
      resultBox.textContent = "正在后台出题…（可先做别的）";
      findBox.style.display = "none";
      await submitAndPoll("/api/practice/generate", { topic, difficulty }, (res) => {
        if (res.result && res.result.html) {
          lastProblems = res.result.problems;
          clearDraft("practice.topic");
          clearDraft("practice.answer");
          resultBox.innerHTML = res.result.html;
          checkPanel.style.display = "block";
          checkResult.style.display = "none";
          myAnswer.value = "";
          myAnswer.__draftShown = "";
        } else {
          resultBox.textContent = res.message || "（出题失败）";
        }
      });
    });

    findBtn.addEventListener("click", async () => {
      const topic = topicInput.value.trim();
      if (!topic) { alert("请先输入知识点"); return; }
      findBox.style.display = "block";
      findBox.textContent = "搜索中…";
      resultBox.style.display = "none";
      const data = await postJSON("/api/practice/find", { topic });
      if (data.ok && data.results.length) {
        findBox.innerHTML = "";
        data.results.forEach((r) => {
          const div = document.createElement("div");
          div.className = "result-item";
          const a = document.createElement("a");
          a.href = r.href; a.target = "_blank"; a.rel = "noopener";
          a.textContent = r.title;
          const sn = document.createElement("div");
          sn.className = "snippet";
          sn.textContent = r.body;
          div.appendChild(a); div.appendChild(sn);
          findBox.appendChild(div);
        });
      } else {
        findBox.textContent = "（没搜到，换个关键词试试）";
      }
    });

    checkBtn.addEventListener("click", async () => {
      const topic = topicInput.value.trim();
      if (!lastProblems) return;
      checkResult.style.display = "block";
      checkResult.textContent = "正在批改…";
      await submitAndPoll("/api/practice/check", { topic, problems: lastProblems, answer: myAnswer.value }, (res) => {
        if (res.result && res.result.html) {
          clearDraft("practice.answer");         // 批改完成 → 答案草稿作废
          myAnswer.__draftShown = "";
        }
        checkResult.innerHTML = (res.result && res.result.html) || res.message || "（出错了）";
      });
    });
  }

  // ===== 拓展 =====
  const extendGenBtn = document.getElementById("extend-gen-btn");
  if (extendGenBtn) {
    const topicInput = document.getElementById("extend-topic");
    const resultBox = document.getElementById("extend-result");
    const saveBox = document.getElementById("extend-save");
    let lastExtend = "";

    extendGenBtn.addEventListener("click", async () => {
      const topic = topicInput.value.trim();
      if (!topic) { alert("请先输入知识点"); return; }
      resultBox.style.display = "block";
      resultBox.textContent = "正在后台生成拓展方向…（可先做别的）";
      saveBox.style.display = "none";
      await submitAndPoll("/api/extend", { topic }, (res) => {
        if (res.result && res.result.html) {
          lastExtend = res.result.extend;
          clearDraft("extend.topic");
          resultBox.innerHTML = res.result.html;
          saveBox.style.display = "block";
          saveBox.innerHTML = '<button class="btn" id="save-extend">保存为笔记</button>';
          document.getElementById("save-extend").addEventListener("click", async () => {
            const r = await postJSON("/api/extend/save", { title: topic + " 拓展", content: lastExtend });
            await guardSave(r);
          });
        } else {
          resultBox.textContent = res.message || "（生成失败）";
        }
      });
    });
  }

  // ===== 找资源 =====
  const resBtn = document.getElementById("res-btn");
  if (resBtn) {
    const topicInput = document.getElementById("res-topic");
    const papersBox = document.getElementById("res-papers");
    const htmlBox = document.getElementById("res-html");

    resBtn.addEventListener("click", async () => {
      const topic = topicInput.value.trim();
      if (!topic) { alert("请先输入方向"); return; }
      papersBox.innerHTML = "";
      htmlBox.style.display = "block";
      htmlBox.textContent = "正在后台找资源…（可先做别的，结果也会出现在「任务」页）";
      await submitAndPoll("/api/resources", { topic }, (res) => {
        if (res.result) {
          clearDraft("resources.topic");
          if (res.result.papers_html) {
            papersBox.innerHTML = res.result.papers_html;
          }
          htmlBox.style.display = "block";
          htmlBox.innerHTML = res.result.html || res.message || "（失败）";
        } else {
          htmlBox.textContent = res.message || "（失败）";
        }
      });
    });
  }

  // ===== 互通：笔记 <-> LLM Wiki =====
  const toWikiBtn = document.getElementById("to-wiki-btn");
  if (toWikiBtn) {
    toWikiBtn.addEventListener("click", async () => {
      const filename = toWikiBtn.dataset.filename;
      const r = await postJSON("/api/note/towiki", { filename });
      alert(r.message || (r.ok ? "已存到 LLM Wiki" : "失败"));
    });
  }
  const toNoteBtn = document.getElementById("to-note-btn");
  if (toNoteBtn) {
    toNoteBtn.addEventListener("click", async () => {
      const relpath = toNoteBtn.dataset.relpath;
      const r = await postJSON("/api/wiki/tonote", { relpath });
      if (r.url) window.location = r.url;
      else alert(r.message || "失败");
    });
  }

  // ===== 学习方法 =====
  const methodBtn = document.getElementById("method-btn");
  if (methodBtn) {
    const subjectInput = document.getElementById("method-subject");
    const resultBox = document.getElementById("method-result");
    const saveBox = document.getElementById("method-save");
    let level = "基础";
    let goal = "综合";
    let lastMethod = "";

    document.querySelectorAll(".subject-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        document.querySelectorAll(".subject-chip").forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
        subjectInput.value = chip.dataset.subject;
      });
    });
    document.querySelectorAll(".level-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        document.querySelectorAll(".level-chip").forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
        level = chip.dataset.level;
      });
    });
    document.querySelectorAll(".goal-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        document.querySelectorAll(".goal-chip").forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
        goal = chip.dataset.goal;
      });
    });

    methodBtn.addEventListener("click", async () => {
      const subject = subjectInput.value.trim();
      if (!subject) { alert("请选择或输入学科"); return; }
      resultBox.style.display = "block";
      resultBox.textContent = "正在后台生成学习方法…（可先做别的）";
      saveBox.style.display = "none";
      await submitAndPoll("/api/method", { subject, level, goal }, (res) => {
        if (res.result && res.result.html) {
          lastMethod = res.result.method;
          clearDraft("method.subject");
          resultBox.innerHTML = res.result.html;
          saveBox.style.display = "block";
          saveBox.innerHTML = '<button class="btn" id="save-method">保存为笔记</button>';
          document.getElementById("save-method").addEventListener("click", async () => {
            const r = await postJSON("/api/method/save", { title: subject + " 学习方法", content: lastMethod });
            await guardSave(r);
          });
        } else {
          resultBox.textContent = res.message || "（生成失败）";
        }
      });
    });
  }

  // ===== 成就页：掌握状态切换 =====
  document.querySelectorAll(".mastery").forEach((el) => {
    el.addEventListener("click", async () => {
      const filename = el.dataset.filename;
      const level = (parseInt(el.dataset.level, 10) % 3) + 1;
      const r = await postJSON("/api/mastery", { filename, level });
      if (r.ok) location.reload();
    });
  });

  // ===== 一键梳理 =====
  const reviewBtn = document.getElementById("review-btn");
  if (reviewBtn) {
    const box = document.getElementById("review-result");
    reviewBtn.addEventListener("click", async () => {
      box.style.display = "block";
      box.textContent = "正在后台梳理…（可先做别的）";
      await submitAndPoll("/api/review", {}, (res) => {
        if (res.result && res.result.html) {
          box.innerHTML = res.result.html;
        } else {
          box.textContent = res.message || "（失败）";
        }
      });
    });
  }

  // ===== 分享成就 =====
  const shareBtn = document.getElementById("share-btn");
  if (shareBtn) {
    shareBtn.addEventListener("click", async () => {
      const r = await fetch("/api/share");
      const j = await r.json();
      const box = document.getElementById("share-result");
      box.style.display = "block";
      box.innerHTML = '<p style="margin:0 0 8px">文案：</p><p>' + j.text.replace(/\n/g, "<br>") + '</p>' +
        '<div style="margin-top:12px; display:flex; gap:10px">' +
        '<button class="btn" id="copy-btn">复制文案</button>' +
        '<a class="btn ghost" href="/api/share/image">下载分享图</a></div>';
      document.getElementById("copy-btn").addEventListener("click", () => {
        navigator.clipboard.writeText(j.text).then(() => alert("已复制，去粘贴分享吧！"));
      });
    });
  }

  // ===== 任务中心 =====
  const tasksList = document.getElementById("tasks-list");
  if (tasksList) {
    async function refreshTasks() {
      const r = await fetch("/api/jobs");
      const j = await r.json();
      const jobs = j.jobs || [];
      if (!jobs.length) {
        tasksList.innerHTML = '<p style="color:var(--muted)">还没有任务。去「学习路线」「练习」「丢书检索」发起一个任务吧。</p>';
        return;
      }
      tasksList.innerHTML = jobs.map((job) => {
        let body = "";
        if (job.status === "running") {
          body = '<div class="snippet">⏳ 运行中…</div>';
        } else if (job.status === "error") {
          body = '<div class="snippet">❌ ' + ((job.result && job.result.message) || "失败") + '</div>';
        } else if (job.result) {
          if (job.result.html) {
            body = '<div class="markdown" style="margin-top:8px">' + job.result.html + '</div>';
          }
          if (job.result.message) {
            body += '<div class="snippet">' + job.result.message + '</div>';
          }
          if (job.result.note_url) {
            body += '<a class="btn" href="' + job.result.note_url + '" style="margin-top:8px">查看笔记</a>';
          }
        }
        return '<div class="note-item"><div class="meta">' + job.created + ' · ' + job.title + '</div>' + body + '</div>';
      }).join("");
    }
    refreshTasks();
    setInterval(refreshTasks, 2000);
  }

  // ===== 使用记录 =====
  const activityList = document.getElementById("activity-list");
  if (activityList) {
    async function loadActivity() {
      const r = await fetch("/api/activity");
      const j = await r.json();
      const records = j.records || [];
      if (!records.length) {
        activityList.innerHTML = '<p style="color:var(--muted)">还没有使用记录。用一用工作台，记录会自动出现。</p>';
        return;
      }
      activityList.innerHTML = records.map((rec, i) => {
        return '<div class="note-item" style="display:flex; justify-content:space-between; align-items:center; gap:12px">' +
          '<div style="min-width:0"><div class="meta">' + esc(rec.time) + '</div>' +
          '<span class="tag">' + esc(rec.action) + '</span> ' +
          '<span style="color:var(--muted); font-size:14px">' + esc(rec.detail) + '</span></div>' +
          '<button class="btn ghost del-rec" data-index="' + i + '" style="padding:4px 10px; font-size:13px; flex-shrink:0">删除</button>' +
          '</div>';
      }).join("");
      document.querySelectorAll(".del-rec").forEach((btn) => {
        btn.addEventListener("click", async () => {
          await postJSON("/api/activity/delete", { index: parseInt(btn.dataset.index, 10) });
          loadActivity();
        });
      });
    }

    const recordToggle = document.getElementById("record-toggle");
    if (recordToggle) {
      recordToggle.addEventListener("change", async () => {
        await postJSON("/api/activity/toggle", { enabled: recordToggle.checked });
        alert(recordToggle.checked ? "已开启记录保存" : "已暂停记录保存");
      });
    }

    // ----- AI 接入设置 -----
    const saveLlmBtn = document.getElementById("save-llm-btn");
    if (saveLlmBtn) {
      saveLlmBtn.addEventListener("click", async () => {
        const payload = {
          provider: document.getElementById("provider").value,
          model: document.getElementById("model").value.trim(),
          base_url: document.getElementById("base_url").value.trim(),
          api_key: document.getElementById("api-key").value.trim(),
        };
        const r = await postJSON("/api/settings", payload);
        if (r.ok) {
          let msg = "AI 接入已保存 ✓ 当前：" + (r.settings.provider || "?") + " / " + (r.settings.model || "?");
          if (r.warnings && r.warnings.length) msg += "\n\n提示：\n" + r.warnings.join("\n");
          alert(msg);
        } else {
          alert("保存失败");
        }
      });
    }

    // ----- 测试连接 -----
    const testBtn = document.getElementById("test-llm-btn");
    if (testBtn) {
      testBtn.addEventListener("click", async () => {
        // 先保存当前表单，再发一个 ask 请求打一发试运行
        const payload = {
          provider: document.getElementById("provider").value,
          model: document.getElementById("model").value.trim(),
          base_url: document.getElementById("base_url").value.trim(),
          api_key: document.getElementById("api-key").value.trim(),
        };
        const box = document.getElementById("test-result");
        box.textContent = "测试中…";
        box.style.color = "var(--muted)";
        try {
          await postJSON("/api/settings", payload);
          const r = await postJSON("/api/ask", { question: "请用一句话（≤10 字）回复：pong" });
          if (r.ok && r.answer) {
            if (r.answer.indexOf("内置演示") !== -1) {
              box.textContent = "ℹ 当前是内置演示（没配真实 Key 时自动使用）。填好真实 Key 保存后再点一次测试。";
              box.style.color = "var(--muted)";
            } else if (!/出错|无效|失败|网络/.test(r.answer)) {
              box.textContent = "✓ 连通正常，模型回： " + r.answer.trim();
              box.style.color = "#2a7a4d";
            } else {
              box.textContent = "✗ " + r.answer.trim();
              box.style.color = "#b04a3a";
            }
          } else {
            box.textContent = "✗ " + ((r && r.message) || "未知错误");
            box.style.color = "#b04a3a";
          }
        } catch (e) {
          box.textContent = "✗ 网络异常：" + e;
          box.style.color = "#b04a3a";
        }
      });
    }

    // ----- 一键填入预设 -----
    const presetBtn = document.getElementById("preset-btn");
    const presetMenu = document.getElementById("preset-menu");
    if (presetBtn && presetMenu) {
      let presets = [];
      async function loadPresets() {
        if (presets.length) return presets;
        const r = await fetch("/api/llm/presets");
        const j = await r.json();
        presets = j.presets || [];
        return presets;
      }
      presetBtn.addEventListener("click", async () => {
        const items = await loadPresets();
        if (!items.length) { alert("预设列表为空"); return; }
        presetMenu.innerHTML = items.map((p, i) =>
          `<div class="preset-item" data-i="${i}" style="padding:8px 12px; cursor:pointer; border-bottom:1px solid var(--border)">
            <strong>${p.label}</strong>
            <div style="font-size:12px; color:var(--muted)">${p.provider} · ${p.model} · ${p.base_url || "(官方)"}</div>
          </div>`
        ).join("");
        presetMenu.style.display = "block";
        presetMenu.querySelectorAll(".preset-item").forEach((el) => {
          el.addEventListener("click", () => {
            const p = items[parseInt(el.dataset.i, 10)];
            document.getElementById("provider").value = p.provider;
            document.getElementById("model").value = p.model;
            document.getElementById("base_url").value = p.base_url || "";
            presetMenu.style.display = "none";
          });
        });
      });
      // 点击外面关闭
      document.addEventListener("click", (e) => {
        if (!presetBtn.contains(e.target) && !presetMenu.contains(e.target)) {
          presetMenu.style.display = "none";
        }
      });
    }

    // ----- Wiki 路径 -----
    const saveWikiBtn = document.getElementById("save-wiki-btn");
    if (saveWikiBtn) {
      saveWikiBtn.addEventListener("click", async () => {
        const v = document.getElementById("wiki_dir").value.trim();
        const r = await postJSON("/api/settings", { wiki_dir: v });
        if (r.ok) alert("Wiki 路径已保存 ✓ 当前：" + (r.settings.wiki_dir || ""));
      });
    }

    const cleanupPeriod = document.getElementById("cleanup-period");
    if (cleanupPeriod) {
      cleanupPeriod.addEventListener("change", async () => {
        await postJSON("/api/settings", { cleanup_period: cleanupPeriod.value });
        alert("清理周期已设置");
      });
    }

    const cleanupBtn = document.getElementById("cleanup-btn");
    if (cleanupBtn) {
      cleanupBtn.addEventListener("click", async () => {
        const r = await postJSON("/api/cleanup", {});
        alert(r.message || "清理完成");
      });
    }

    const clearBtn = document.getElementById("clear-btn");
    if (clearBtn) {
      clearBtn.addEventListener("click", async () => {
        if (!confirm("确定清空所有使用记录？")) return;
        await postJSON("/api/activity/clear", {});
        loadActivity();
      });
    }

    loadActivity();
  }
});
