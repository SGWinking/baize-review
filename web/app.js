/* ==========================================================================
   大云壁画工具箱 · 白泽评审 Baize Review
   app.js —— 状态、API 客户端、工作流

   本文件由旧版 web/index.html 内联脚本拆分而来，**业务逻辑保持等价**：
   同样的接口、同样的调用顺序、同样的打分与 mask 人工修正流程。
   仅有两处适配：
     1. 错误响应改为底座的统一结构 `{error:{code,message,...}}`；
     2. 页面切换由 class 改为 hidden 属性，页头/底栏元素重新命名。
   ========================================================================== */

const $ = (id) => document.getElementById(id);
const pages = { import: $("importStep"), mask: $("maskStep"), score: $("scoreStep") };
const steps = document.querySelectorAll(".nav-tab");
let originalUpload = null, candidateUploads = [], currentProject = null, currentMask = null;
let strokes = [], activeStroke = null, paintCtx = $("paintCanvas").getContext("2d");
let scoreResult = null, selectedCandidate = "";

const maskLabels = {
  "final_mask_preview.png": "最终预览", "mask_preview.png": "自动预览",
  "evidence_mask.png": "证据区", "line_evidence_mask.png": "线条证据", "pigment_evidence_mask.png": "残色证据",
  "damage_mask.png": "损伤区", "missing_mask.png": "缺失区", "mud_or_occlusion_mask.png": "泥层/遮挡",
  "blank_plaster_mask.png": "空白灰泥", "uncertain_mask.png": "不确定区"
};
const maskDesc = {
  "final_mask_preview.png": "自动 mask + 人工 override 的最终评分预览。",
  "mask_preview.png": "绿色=证据，橙色=损伤/低证据，蓝紫色=不确定。",
  "evidence_mask.png": "原图中仍有可参考的线条/纹样/颜料证据。",
  "line_evidence_mask.png": "原图中可见线条、边缘、轮廓的区域。",
  "pigment_evidence_mask.png": "原图中仍有残色或颜料方向的区域。",
  "damage_mask.png": "由缺失/遮挡/空白灰泥合成的损伤低证据区。",
  "missing_mask.png": "信息不足或缺失较明显的区域。",
  "mud_or_occlusion_mask.png": "疑似泥层、污渍、遮挡物覆盖的区域。",
  "blank_plaster_mask.png": "大面积空白灰泥或无彩绘痕迹区域。",
  "uncertain_mask.png": "自动判断不够稳定的区域，评分时降信任。"
};
const maskGroups = {
  "预览": ["final_mask_preview.png", "mask_preview.png"],
  "证据类": ["evidence_mask.png", "line_evidence_mask.png", "pigment_evidence_mask.png"],
  "损伤/低证据": ["damage_mask.png", "missing_mask.png", "mud_or_occlusion_mask.png", "blank_plaster_mask.png"],
  "不确定": ["uncertain_mask.png"]
};
const scoreLabels = {
  lineFidelity: "线条忠实度", structure: "结构保持", pigmentDirection: "残色方向一致性",
  damageRestraint: "损伤区克制", anomaly: "异常风险控制", completeness: "完整度", displayReadability: "展示可读性",
  colorExpression: "色彩表现", exhibitionClarity: "展陈清晰度", surfaceCoherence: "表面统一", styleUnity: "风格统一",
  composition: "构图稳定", evidenceCompat: "证据兼容综合", speculationControl: "推测风险综合"
};
const visibleScoreKeys = [
  "lineFidelity", "structure", "pigmentDirection", "damageRestraint", "anomaly", "completeness",
  "displayReadability", "colorExpression", "exhibitionClarity", "surfaceCoherence", "styleUnity", "composition"
];
const scoreHelp = {
  conservativeTotal: "保守榜总分 = 线条 39% + 结构 12% + 残色方向 8% + 损伤克制 25% + 异常风险 16% - 鲜艳 caution。偏向修旧如旧。",
  displayTotal: "展示榜总分 = 完整 4% + 可读性 16% + 色彩 10% + 展陈清晰 38% + 表面统一 12% + 风格 4% + 构图 6% + 证据兼容 6% + 推测风险 4%。",
  lineFidelity: "看候选线条是否落在原 line mask 附近；原线条位置保留越好、加固越锚定，分越高。",
  structure: "主要看 evidence mask 内结构和原线条位置是否保持，降低对亮度增强的惩罚。",
  pigmentDirection: "在 pigment mask 内比较色相方向，允许亮度和饱和度增强，主要扣冷暖跑偏。",
  damageRestraint: "看 damage/低证据区新增线条是否有原始证据锚定；凭空新增花纹重扣。",
  anomaly: "看非损伤区边缘密度、uncertain 区变化、异常高饱和和低证据新增线条。",
  completeness: "在 damage mask 内估计缺损区是否被合理填补，扣除无锚定新增线条。",
  displayReadability: "奖励残色区合理饱和度提升、对比度提升、原线条仍可读。",
  colorExpression: "奖励 pigment/damage 区更鲜明可展陈的色彩表现，仍受证据兼容约束。",
  exhibitionClarity: "奖励展陈观看清晰度，由对比度提升和较少噪声边缘共同决定。",
  surfaceCoherence: "看候选图纹理密度是否平顺统一，减少块状突兀或局部割裂。",
  styleUnity: "主要惩罚局部块状跳变、区域割裂和低证据区突兀新增。",
  composition: "看主要线条位置召回和 evidence 区结构稳定，允许一定颜色/对比度增强。",
  evidenceCompat: "综合项：线条 36% + 结构 34% + 残色方向 18% + 损伤克制 12%。",
  speculationControl: "综合项：损伤克制 72% + 异常风险 28%。"
};
const metricLabels = {
  line_position_recall: "原线条位置召回率", line_strength_gain: "原线条加固强度",
  line_anchored_gain: "线条锚定增强比例", line_gradient_delta_mean: "line 梯度差均值",
  evidence_luminance_delta_mean: "evidence 亮度差均值", evidence_gradient_delta_mean: "evidence 梯度差均值",
  pigment_hue_delta_mean: "pigment 色相方向差均值", pigment_cool_warm_flip_ratio: "pigment 冷暖翻转比例",
  damage_added_gradient_mean: "damage 新增梯度均值", unanchored_damage_edge_ratio: "损伤区无锚定新增线条比例",
  low_evidence_new_edge_ratio: "低证据区新增线条比例", evidence_saturation_gain: "残色区饱和度提升",
  candidate_pigment_saturation: "候选 pigment 区饱和度", candidate_damage_saturation: "候选 damage 区饱和度",
  global_contrast_gain: "全图对比度提升", candidate_edge_density: "候选图边缘密度",
  conservative_vivid_caution: "保守榜鲜艳 caution", non_damage_edge_density_delta: "非损伤区边缘密度变化",
  abnormal_saturation_mean: "异常高饱和均值", candidate_saturation_block_std: "饱和度块状波动",
  candidate_value_block_std: "亮度块状波动", candidate_gradient_block_std: "纹理密度块状波动",
  damage_evidence_saturation_gap: "损伤/证据区饱和度落差"
};
const metricHelp = {
  line_position_recall: "原 line mask 强边缘区中候选附近仍能找到线条的比例。越高越好。",
  line_strength_gain: "原线条区域内候选相对原图的梯度增强，适度增强视为加固。",
  line_anchored_gain: "候选新增边缘落在原线条附近的比例。越高越有锚定。",
  line_gradient_delta_mean: "line mask 内 abs(原图梯度-候选梯度) 均值，越低越好。",
  evidence_luminance_delta_mean: "evidence mask 内亮度差均值，越低越接近。",
  evidence_gradient_delta_mean: "evidence mask 内梯度差均值，越低越稳定。",
  pigment_hue_delta_mean: "pigment mask 内色相环形距离均值，越低越一致。",
  pigment_cool_warm_flip_ratio: "pigment mask 内冷暖翻转比例，越低越好。",
  damage_added_gradient_mean: "damage mask 内 max(候选-原图梯度,0) 均值，越高新增纹理越多。",
  unanchored_damage_edge_ratio: "damage 内远离原证据的候选边缘比例，越高越可能凭空新增。",
  low_evidence_new_edge_ratio: "低证据区新增边缘比例，越高越不克制。",
  evidence_saturation_gain: "残色区饱和度提升，展示榜适度奖励。",
  candidate_pigment_saturation: "候选 pigment 区平均饱和度。",
  candidate_damage_saturation: "候选 damage 区平均饱和度。",
  global_contrast_gain: "候选相对原图整体对比度提升，展示榜适度奖励。",
  candidate_edge_density: "候选全图平均边缘密度，较少噪声边缘=更清爽。",
  conservative_vivid_caution: "保守榜对过艳的轻微 caution，只在结构接近时区分。",
  non_damage_edge_density_delta: "非 damage 区边缘密度差值，越低越好。",
  abnormal_saturation_mean: "过高饱和度超出阈值的平均量，越高越可能过艳。",
  candidate_saturation_block_std: "分块饱和度均值标准差，越高色彩越跳。",
  candidate_value_block_std: "分块亮度均值标准差，越高明暗越不均。",
  candidate_gradient_block_std: "分块纹理密度标准差，越高纹理越不统一。",
  damage_evidence_saturation_gap: "damage 与 evidence 区饱和度差，过高可能不协调。"
};

/* ------------------------------------------------------------ 基础 UI */

function setStatus(msg, isErr = false) {
  $("status").textContent = msg;
  $("statusbar").classList.toggle("error", isErr);
  const box = $("importStatus");
  if (box) {
    box.textContent = msg;
    box.className = "status" + (isErr ? " err" : " working");
  }
}

function setBusy(b) {
  document.querySelectorAll("button").forEach((x) => { x.disabled = b; });
}

/* 长任务的进行中反馈：触发按钮加转圈并把文字换成进行中文案，结束后恢复。 */
function withBusyButton(btn, label, run) {
  if (!btn) return run();
  if (btn.classList.contains("btn-busy")) return run();
  const original = btn.textContent;
  btn.classList.add("btn-busy");
  btn.textContent = label;
  const restore = () => { btn.classList.remove("btn-busy"); btn.textContent = original; };
  const p = run();
  p.then(restore, restore);
  return p;
}

function log(msg, data) {
  const t = new Date().toLocaleTimeString();
  $("log").textContent += `[${t}] ${msg}\n` + (data ? JSON.stringify(data) + "\n" : "");
  $("log").scrollTop = $("log").scrollHeight;
}

function goStep(name) {
  Object.entries(pages).forEach(([k, p]) => { p.hidden = k !== name; });
  steps.forEach((s) => s.classList.toggle("active", s.dataset.step === name));
  if (name === "mask") syncMaskLayerSoon();
}

steps.forEach((s) => s.addEventListener("click", () => goStep(s.dataset.step)));

/* ------------------------------------------------------------ API 客户端
   底座统一错误结构：{ "error": { "code", "message", "field", "detail" } }
   message 是可直接显示给用户的中文。 */

function errorText(data, response) {
  const err = data && data.error;
  if (err) return typeof err === "string" ? err : (err.message || "请求失败。");
  return (response && response.statusText) || `HTTP ${response ? response.status : "?"}`;
}

async function post(url, payload) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json();
  if (!r.ok || d.error) throw new Error(errorText(d, r));
  return d;
}

async function get(url) {
  const r = await fetch(url);
  const d = await r.json();
  if (!r.ok || d.error) throw new Error(errorText(d, r));
  return d;
}

async function uploadFile(file, role) {
  const r = await fetch("/api/upload", {
    method: "POST",
    headers: { "X-Filename": encodeURIComponent(file.name), "X-Role": role },
    body: file,
  });
  const d = await r.json();
  if (!r.ok || d.error) throw new Error(errorText(d, r));
  return d;
}

/* ------------------------------------------------------------ 健康检查
   页头版本号与端口都取自 /api/health，保证与 server.py 的 APP_VERSION 同源。 */

async function checkHealth() {
  try {
    const h = await get("/api/health");
    $("healthDot").classList.add("ok");
    $("healthDot").classList.remove("bad");
    $("healthDot").title = `ok · ${h.tool || ""} ${h.nameEn || ""} v${h.version} · core ${h.core}`;
    $("healthText").textContent = "正常";
    $("appVersion").textContent = "v" + h.version;
    $("portText").textContent = ":" + h.port;
    return h;
  } catch (e) {
    $("healthDot").classList.add("bad");
    $("healthDot").title = "服务未就绪";
    $("healthText").textContent = "未就绪";
    return null;
  }
}

checkHealth();

/* ------------------------------------------------------------ 拖放 */

function bindDropzone(zone, input) {
  zone.addEventListener("dragenter", (e) => { e.preventDefault(); zone.classList.add("drag"); });
  zone.addEventListener("dragover", (e) => e.preventDefault());
  zone.addEventListener("dragleave", (e) => { if (!zone.contains(e.relatedTarget)) zone.classList.remove("drag"); });
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("drag");
    if (e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      input.dispatchEvent(new Event("change"));
    }
  });
}

document.querySelectorAll(".drop").forEach((z) => bindDropzone(z, z.querySelector("input")));

function markDrop(zoneName, hasFile) {
  const zone = document.querySelector(`.drop[data-dropzone="${zoneName}"]`);
  if (zone) zone.classList.toggle("has-file", Boolean(hasFile));
}

/* ------------------------------------------------------------ 选图 */

$("pickOriginal").addEventListener("click", async () => {
  try {
    const d = await post("/api/pick-file", { kind: "image" });
    if (d.path) {
      $("originalInput").value = "";
      originalUpload = { path: d.path, name: d.path.split(/[\\/]/).pop() };
      $("originalName").textContent = originalUpload.name;
      markDrop("original", true);
      log("已选原图", { path: d.path });
    }
  } catch (e) { log("选原图失败：" + e.message); }
});

$("pickCandidates").addEventListener("click", async () => {
  try {
    const d = await post("/api/pick-file", { kind: "image", multi: true });
    const paths = d.paths || [];
    if (paths.length) {
      const dt = new DataTransfer();
      for (const p of paths) {
        const blob = await (await fetch("file:///" + p.replace(/\\/g, "/"))).blob().catch(() => null);
        if (blob) {
          const f = new File([blob], p.split(/[\\/]/).pop(), { type: blob.type });
          dt.items.add(f);
        }
      }
      $("candidateInput").files = dt.files;
      $("candidateInput").dispatchEvent(new Event("change"));
      log("已选候选", { count: paths.length });
    }
  } catch (e) { log("选候选失败：" + e.message); }
});

/* ------------------------------------------------------------ 候选列表 */

function updateCandidateList() {
  const list = $("candidateList");
  list.textContent = "";
  const files = [...$("candidateInput").files];
  $("candidateCount").textContent = String(files.length);
  $("candidateName").textContent = files.length ? `${files.length} 张候选图` : "拖入或点击选择";
  markDrop("candidate", files.length > 0);
  if (!files.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "还没有导入候选图。";
    list.appendChild(empty);
    return;
  }
  for (const f of files) {
    const it = document.createElement("div");
    it.className = "file-item";
    it.innerHTML = `<strong>${f.name}</strong><span>${(f.size / 1024 / 1024).toFixed(2)} MB</span>`;
    list.appendChild(it);
  }
}

$("originalInput").addEventListener("change", () => {
  const f = $("originalInput").files[0];
  $("originalName").textContent = f ? f.name : "拖入或点击选择";
  markDrop("original", Boolean(f));
  if (f) $("originalHistory").value = "";
});

$("candidateInput").addEventListener("change", updateCandidateList);

/* ------------------------------------------------------------ 原图历史 */

async function loadOriginalHistory() {
  try {
    const d = await get("/api/history");
    const items = d.originals || [];
    $("originalHistory").innerHTML = items.length
      ? `<option value="">选择历史原图</option>${items.map((i) => `<option value="${i.path}">${i.name} | ${i.width}x${i.height} | ${i.addedAt}</option>`).join("")}`
      : `<option value="">暂无历史</option>`;
  } catch {
    $("originalHistory").innerHTML = `<option value="">历史读取失败</option>`;
  }
}

$("originalHistory").addEventListener("change", () => {
  if ($("originalHistory").value) {
    $("originalInput").value = "";
    const t = $("originalHistory").selectedOptions[0]?.textContent || "历史原图";
    $("originalName").textContent = t.split(" | ")[0];
    markDrop("original", true);
  }
});

/* ------------------------------------------------------------ 尺寸对齐记录 */

function sizeText(i) { return `${i.source_width ?? i.width} x ${i.source_height ?? i.height}`; }
function targetSizeText(i) { return `${i.width} x ${i.height}`; }

function renderResizeLog(p) {
  const box = $("resizeLog");
  box.textContent = "";
  const rows = [{ label: "原图", item: p.original }, ...p.candidates.map((i) => ({ label: i.name, item: i }))];
  for (const r of rows) {
    const ch = Boolean(r.item.normalized);
    const el = document.createElement("div");
    el.className = `resize-item${ch ? " changed" : ""}`;
    el.innerHTML =
      `<strong>${r.label}</strong>` +
      `<span><small>原尺寸</small> ${sizeText(r.item)}</span>` +
      `<span class="arrow">→</span>` +
      `<span><small>评分</small> ${targetSizeText(r.item)}</span>` +
      `<em class="badge">${ch ? "已对齐" : "未变化"}</em>`;
    box.appendChild(el);
  }
}

/* ------------------------------------------------------------ 上传与建项目 */

async function gatherUploads() {
  if (!$("originalInput").files[0] && !$("originalHistory").value) throw new Error("请先选择原图");
  if (!$("candidateInput").files.length) throw new Error("请至少选择一张候选图");
  originalUpload = $("originalHistory").value
    ? { path: $("originalHistory").value, name: $("originalName").textContent }
    : await uploadFile($("originalInput").files[0], "original");
  candidateUploads = [];
  let i = 0;
  const total = $("candidateInput").files.length;
  for (const f of $("candidateInput").files) {
    i++;
    setStatus(`上传候选图 ${i}/${total}`);
    candidateUploads.push(await uploadFile(f, `candidate_${i}`));
  }
}

function applyProjectToImportUI(project) {
  $("originalSize").textContent = `${project.original.width} x ${project.original.height}`;
  $("candidateCount").textContent = String(project.candidates.length);
  const ch = [project.original, ...project.candidates].filter((i) => i.normalized).length;
  $("sizeCheck").textContent = ch ? `${ch} 张已对齐` : "均为长边2048";
  $("projectIdText").textContent = `${project.projectName} / ${project.projectId}`;
  renderResizeLog(project);
  $("originalPreview").src = `${project.originalUrl}?t=${Date.now()}`;
}

async function createProject() {
  setBusy(true);
  setStatus("上传原图");
  try {
    await gatherUploads();
    setStatus("创建项目并对齐到长边 2048");
    currentProject = await post("/api/create-project", {
      projectName: $("projectName").value.trim(),
      original: originalUpload.path,
      candidates: candidateUploads.map((i) => i.path),
    });
    applyProjectToImportUI(currentProject);
    setStatus("项目已创建");
    loadOriginalHistory();
    goStep("mask");
    syncMaskLayerSoon();
  } finally {
    setBusy(false);
  }
}

$("createProjectButton").addEventListener("click", () =>
  createProject().catch((e) => { setStatus(e.message, true); setBusy(false); }));

/* ------------------------------------------------------------ Mask 主视图 */

function syncMaskLayer() {
  if (!$("originalPreview").naturalWidth || !$("originalPreview").naturalHeight) return;
  const w = Math.max(1, $("maskStage").clientWidth - 2);
  const h = Math.max(1, $("maskStage").clientHeight - 2);
  const fit = Math.min(w / $("originalPreview").naturalWidth, h / $("originalPreview").naturalHeight);
  const nw = Math.max(1, Math.round($("originalPreview").naturalWidth * fit));
  const nh = Math.max(1, Math.round($("originalPreview").naturalHeight * fit));
  $("maskLayer").style.width = nw + "px";
  $("maskLayer").style.height = nh + "px";
  $("paintCanvas").width = $("originalPreview").naturalWidth;
  $("paintCanvas").height = $("originalPreview").naturalHeight;
  renderStrokes();
}

function syncMaskLayerSoon() { setTimeout(syncMaskLayer, 60); }

$("originalPreview").addEventListener("load", syncMaskLayer);
window.addEventListener("resize", syncMaskLayer);

$("maskOpacity").addEventListener("input", () => {
  const v = Number($("maskOpacity").value) / 100;
  $("maskOpacityText").textContent = $("maskOpacity").value + "%";
  $("maskPreview").style.opacity = String(v);
});

function pct(v) { return typeof v !== "number" ? "-" : `${(v * 100).toFixed(2)}%`; }

function updateMaskMetrics(m) {
  $("evidenceRatio").textContent = pct(m.evidence_ratio);
  $("damageRatio").textContent = pct(m.damage_ratio);
  $("uncertainRatio").textContent = pct(m.uncertain_ratio);
  $("maskConfidence").textContent = m.mask_confidence || "-";
}

function infoPop(t) {
  return `<details class="info-pop"><summary aria-label="说明">!</summary><div>${t}</div></details>`;
}

function renderMaskToggles(data) {
  const box = $("maskToggles");
  box.textContent = "";
  const urls = { "final_mask_preview.png": data.finalPreviewUrl, "mask_preview.png": data.maskPreviewUrl, ...data.maskUrls };
  let idx = 0;
  Object.entries(maskGroups).forEach(([g, names]) => {
    const t = document.createElement("div");
    t.className = "mask-group-title";
    t.textContent = g;
    box.appendChild(t);
    names.forEach((n) => {
      const u = urls[n];
      if (!u) return;
      const lb = document.createElement("label");
      lb.className = "mask-toggle";
      lb.innerHTML = `<span class="label-info">${maskLabels[n] || n}${infoPop(maskDesc[n] || "")}</span><input type="radio" name="maskView" ${idx === 0 ? "checked" : ""}>`;
      lb.addEventListener("click", () => { $("maskPreview").src = `${u}?t=${Date.now()}`; });
      box.appendChild(lb);
      idx++;
    });
  });
}

async function generateMask() {
  if (!currentProject) throw new Error("请先创建项目");
  setBusy(true);
  setStatus("生成 mask");
  try {
    currentMask = await post("/api/generate-mask", {
      projectId: currentProject.projectId,
      edgeSensitivity: Number($("edgeSensitivity").value),
      pigmentSensitivity: Number($("pigmentSensitivity").value),
      missingSensitivity: Number($("missingSensitivity").value),
    });
    $("maskPreview").src = `${currentMask.finalPreviewUrl}?t=${Date.now()}`;
    updateMaskMetrics(currentMask.metrics);
    renderMaskToggles(currentMask);
    strokes = [];
    renderStrokes();
    syncMaskLayerSoon();
    setStatus("mask 已生成");
    log("mask 生成", { evidence: pct(currentMask.metrics.evidence_ratio) });
  } finally {
    setBusy(false);
  }
}

$("generateMaskButton").addEventListener("click", () =>
  generateMask().catch((e) => { setStatus(e.message, true); setBusy(false); }));

/* ------------------------------------------------------------ 画笔 / 矩形 */

function currentPaintMode() { return document.querySelector('input[name="paintMode"]:checked')?.value || "evidence"; }
function currentPaintTool() { return document.querySelector('input[name="paintTool"]:checked')?.value || "brush"; }

function colorForMode(m) {
  if (m === "damage") return "rgba(249,115,22,0.48)";
  if (m === "uncertain") return "rgba(124,58,237,0.42)";
  if (m === "erase") return "rgba(255,255,255,0.55)";
  return "rgba(37,99,235,0.45)";
}

function pointerToImage(e) {
  const r = $("paintCanvas").getBoundingClientRect();
  return {
    x: Math.max(0, Math.min($("paintCanvas").width, ((e.clientX - r.left) / r.width) * $("paintCanvas").width)),
    y: Math.max(0, Math.min($("paintCanvas").height, ((e.clientY - r.top) / r.height) * $("paintCanvas").height)),
  };
}

function drawStroke(ctx, s) {
  const p = s.points || [];
  if (!p.length) return;
  ctx.save();
  ctx.strokeStyle = colorForMode(s.mode);
  ctx.fillStyle = colorForMode(s.mode);
  ctx.lineWidth = s.size;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  if (s.tool === "rect" && p.length >= 2) {
    const a = p[0], b = p[p.length - 1];
    ctx.fillRect(a.x, a.y, b.x - a.x, b.y - a.y);
  } else if (p.length === 1) {
    const q = p[0];
    ctx.beginPath();
    ctx.arc(q.x, q.y, s.size / 2, 0, Math.PI * 2);
    ctx.fill();
  } else {
    ctx.beginPath();
    ctx.moveTo(p[0].x, p[0].y);
    for (const q of p.slice(1)) ctx.lineTo(q.x, q.y);
    ctx.stroke();
  }
  ctx.restore();
}

function renderStrokes() {
  paintCtx.clearRect(0, 0, $("paintCanvas").width, $("paintCanvas").height);
  for (const s of strokes) drawStroke(paintCtx, s);
  if (activeStroke) drawStroke(paintCtx, activeStroke);
}

$("paintCanvas").addEventListener("pointerdown", (e) => {
  if (!$("paintCanvas").width) return;
  $("paintCanvas").setPointerCapture(e.pointerId);
  activeStroke = {
    mode: currentPaintMode(),
    tool: currentPaintTool(),
    size: Number($("brushSize").value),
    points: [pointerToImage(e)],
  };
  renderStrokes();
});

$("paintCanvas").addEventListener("pointermove", (e) => {
  if (!activeStroke) return;
  const p = pointerToImage(e);
  if (activeStroke.tool === "rect") activeStroke.points = [activeStroke.points[0], p];
  else activeStroke.points.push(p);
  renderStrokes();
});

$("paintCanvas").addEventListener("pointerup", () => {
  if (!activeStroke) return;
  strokes.push(activeStroke);
  activeStroke = null;
  renderStrokes();
});

$("brushSize").addEventListener("input", () => {
  $("brushSizeText").textContent = $("brushSize").value;
  updateBrushCursor();
});

function updateBrushCursor(e = null) {
  if (!$("brushCursor") || !$("paintCanvas").width) return;
  const r = $("paintCanvas").getBoundingClientRect();
  const base = Math.max(1, r.width / $("paintCanvas").width);
  const sz = Math.max(6, Number($("brushSize").value) * base);
  $("brushCursor").style.width = sz + "px";
  $("brushCursor").style.height = sz + "px";
  $("brushCursor").className = `brush-cursor mode-${currentPaintMode()}`;
  if (e) {
    $("brushCursor").style.left = (e.clientX - r.left) + "px";
    $("brushCursor").style.top = (e.clientY - r.top) + "px";
  }
}

$("paintCanvas").addEventListener("pointerenter", (e) => { $("brushCursor").hidden = false; updateBrushCursor(e); });
$("paintCanvas").addEventListener("pointerleave", () => { $("brushCursor").hidden = true; });
$("paintCanvas").addEventListener("pointermove", updateBrushCursor);
document.querySelectorAll('input[name="paintMode"]').forEach((i) => i.addEventListener("change", updateBrushCursor));

$("undoButton").addEventListener("click", () => { strokes.pop(); renderStrokes(); });
$("clearStrokesButton").addEventListener("click", () => { strokes = []; renderStrokes(); });

/* ------------------------------------------------------------ 人工修正保存 */

async function saveOverride() {
  if (!currentProject || !currentMask) throw new Error("请先创建项目并生成 mask");
  setBusy(true);
  setStatus("保存人工修正");
  try {
    const r = await post("/api/save-override", { projectId: currentProject.projectId, strokes });
    $("maskPreview").src = `${r.finalPreviewUrl}?t=${Date.now()}`;
    $("evidenceRatio").textContent = pct(r.evidence_ratio);
    $("damageRatio").textContent = pct(r.damage_ratio);
    $("uncertainRatio").textContent = pct(r.uncertain_ratio);
    setStatus("人工修正已保存");
    log("override 已保存");
  } finally {
    setBusy(false);
  }
}

$("saveOverrideButton").addEventListener("click", () =>
  saveOverride().catch((e) => { setStatus(e.message, true); setBusy(false); }));

async function resetOverride() {
  if (!currentProject) throw new Error("请先创建项目");
  if (!confirm("回到自动 mask 会丢弃当前所有人工修正，确定？")) return;
  setBusy(true);
  setStatus("回到自动 mask");
  try {
    const r = await post("/api/reset-override", { projectId: currentProject.projectId });
    currentMask = {
      metrics: r.metrics,
      finalPreviewUrl: r.finalPreviewUrl,
      maskPreviewUrl: r.maskPreviewUrl,
      maskUrls: currentMask?.maskUrls,
    };
    $("maskPreview").src = `${r.finalPreviewUrl}?t=${Date.now()}`;
    updateMaskMetrics(r.metrics);
    strokes = [];
    renderStrokes();
    setStatus("已回到自动 mask");
    log("override 已重置");
  } finally {
    setBusy(false);
  }
}

$("resetOverrideButton").addEventListener("click", () =>
  resetOverride().catch((e) => { setStatus(e.message, true); setBusy(false); }));

/* ------------------------------------------------------------ 评分与双榜 */

function scoreBar(label, value, help = "") {
  return `<div class="score-bar"><header><span class="label-info">${label}${help ? infoPop(help) : ""}</span><strong>${value}</strong></header><div class="bar-track"><div class="bar-fill" style="width:${Math.max(0, Math.min(100, value))}%"></div></div></div>`;
}

function renderRanking(target, items, field) {
  target.textContent = "";
  if (!items || !items.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "等待评分";
    target.appendChild(empty);
    return;
  }
  for (const it of items) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `rank-item${selectedCandidate === it.name ? " selected" : ""}`;
    b.innerHTML = `<strong>${it[field === "conservative" ? "conservativeRank" : "displayRank"]}. ${it.name}</strong><span>${it.totals[field]} 分</span><span>${it.risks[0] || ""}</span>`;
    b.addEventListener("click", () => selectCandidate(it.name));
    target.appendChild(b);
  }
}

function renderCompareWall() {
  const wall = $("compareWall");
  wall.textContent = "";
  if (!scoreResult) return;
  for (const it of scoreResult.candidates) {
    const c = document.createElement("div");
    c.className = `cmp-card${selectedCandidate === it.name ? " selected" : ""}`;
    c.innerHTML = `<img src="${it.previewUrl}&t=${Date.now()}" alt="${it.name}"><div class="cmp-meta"><div class="cmp-name">${it.name}</div><div class="cmp-scores"><span>保守 <b>${it.totals.conservative}</b></span><span>展示 <b>${it.totals.display}</b></span></div></div>`;
    c.addEventListener("click", () => selectCandidate(it.name));
    wall.appendChild(c);
  }
}

function selectCandidate(name) {
  if (!scoreResult) return;
  selectedCandidate = name;
  const it = scoreResult.candidates.find((c) => c.name === name);
  if (!it) return;
  $("detailTitle").textContent = it.name;
  $("detailRanks").textContent = `保守 #${it.conservativeRank} / 展示 #${it.displayRank}`;
  $("detailRanks").className = "pill";
  $("detailImage").src = `${it.previewUrl}&t=${Date.now()}`;
  $("scoreBars").innerHTML = [
    scoreBar("保守修复总分", it.totals.conservative, scoreHelp.conservativeTotal),
    scoreBar("展示复原总分", it.totals.display, scoreHelp.displayTotal),
    ...visibleScoreKeys.map((k) => scoreBar(scoreLabels[k] || k, it.scores[k], scoreHelp[k])),
  ].join("");
  $("reasonBox").className = "box";
  $("reasonBox").innerHTML = `<h3>榜单理由</h3><div class="reason-cols"><div><strong>保守修复榜</strong><ul>${(it.reasons?.conservative || [it.explanation]).map((t) => `<li>${t}</li>`).join("")}</ul></div><div><strong>展示复原榜</strong><ul>${(it.reasons?.display || [it.explanation]).map((t) => `<li>${t}</li>`).join("")}</ul></div></div>`;
  $("compositeScores").className = "box";
  $("compositeScores").innerHTML = `<h3>综合项（参与榜单公式，非独立检测项）</h3><div class="mrow"><span class="label-info">${scoreLabels.evidenceCompat}${infoPop(scoreHelp.evidenceCompat)}</span><strong>${it.scores.evidenceCompat}</strong></div><div class="mrow"><span class="label-info">${scoreLabels.speculationControl}${infoPop(scoreHelp.speculationControl)}</span><strong>${it.scores.speculationControl}</strong></div>`;
  $("rawMetrics").className = "box";
  $("rawMetrics").innerHTML = `<h3>原始量化指标</h3>${Object.entries(it.rawMetrics || {}).map(([k, v]) => `<div class="mrow"><span class="label-info">${metricLabels[k] || k}${infoPop(metricHelp[k] || "中间量，用于解释分数来源。")}</span><strong>${v}</strong></div>`).join("")}`;
  $("riskTags").textContent = "";
  it.risks.forEach((r) => {
    const t = document.createElement("span");
    t.textContent = r;
    $("riskTags").appendChild(t);
  });
  $("explanation").textContent = it.explanation;
  renderRanking($("conservativeRanking"), scoreResult.conservativeRanking, "conservative");
  renderRanking($("displayRanking"), scoreResult.displayRanking, "display");
  renderCompareWall();
}

function showReport(r) {
  if (!r?.reportUrl && !r?.reportPath) { $("reportLink").textContent = ""; return; }
  $("reportLink").innerHTML = `<strong>报告已生成</strong>${r.reportPath ? `<span>本地：${r.reportPath}</span>` : ""}${r.roundReportPath ? `<span>本轮归档：${r.roundReportPath}</span>` : ""}${r.reportUrl ? `<a href="${r.reportUrl}" target="_blank">打开报告</a>` : ""}${r.roundReportUrl ? `<a href="${r.roundReportUrl}" target="_blank">本轮归档</a>` : ""}`;
}

function applyScoreResult(r) {
  scoreResult = r;
  selectedCandidate = r.conservativeRanking[0]?.name || "";
  renderRanking($("conservativeRanking"), r.conservativeRanking, "conservative");
  renderRanking($("displayRanking"), r.displayRanking, "display");
  showReport(r);
  renderCompareWall();
  renderRounds(r.calibrationRounds || []);
  if (selectedCandidate) selectCandidate(selectedCandidate);
}

function renderRounds(rounds) {
  const el = $("roundsList");
  el.textContent = "";
  if (!rounds || !rounds.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "暂无轮次";
    el.appendChild(empty);
    return;
  }
  for (const r of rounds) {
    const it = document.createElement("div");
    it.className = "round-item";
    it.innerHTML = `<code>${r.roundId}</code><span>${r.scoredAt || ""} · 保守: ${r.conservativeWinner || "-"} · 展示: ${r.displayWinner || "-"}</span><a href="${r.reportUrl}" target="_blank">报告</a>`;
    el.appendChild(it);
  }
}

async function runScore() {
  if (!currentProject) throw new Error("请先创建项目");
  setBusy(true);
  setStatus("评分中");
  try {
    const r = await post("/api/score", { projectId: currentProject.projectId });
    applyScoreResult(r);
    setStatus("评分完成");
    log("评分完成", { conservative: r.conservativeRanking[0]?.name, display: r.displayRanking[0]?.name });
  } finally {
    setBusy(false);
  }
}

$("scoreButton").addEventListener("click", (e) =>
  withBusyButton(e.currentTarget, "评分中…", () =>
    runScore().catch((e) => { setStatus(e.message, true); setBusy(false); })));

$("loadRoundsButton").addEventListener("click", async () => {
  if (!currentProject) return;
  try {
    const d = await post("/api/calibration-rounds", { projectId: currentProject.projectId });
    renderRounds(d.rounds);
  } catch (e) { setStatus(e.message, true); }
});

$("exportZipButton").addEventListener("click", async () => {
  if (!currentProject) return;
  try {
    setBusy(true);
    const d = await post("/api/export-zip", { projectId: currentProject.projectId });
    setStatus("已导出 zip");
    log("zip 导出", d);
    window.open(d.url, "_blank");
  } catch (e) { setStatus(e.message, true); }
  finally { setBusy(false); }
});

/* ------------------------------------------------------------ 自动评审（一键到底） */

async function autoReview() {
  setBusy(true);
  try {
    await gatherUploads();
    setStatus("自动流程：对齐尺寸、生成 mask、评分");
    const ar = await post("/api/auto-review", {
      projectName: $("projectName").value.trim(),
      original: originalUpload.path,
      candidates: candidateUploads.map((i) => i.path),
      edgeSensitivity: Number($("edgeSensitivity").value),
      pigmentSensitivity: Number($("pigmentSensitivity").value),
      missingSensitivity: Number($("missingSensitivity").value),
    });
    currentProject = ar.project;
    const pid = currentProject.projectId;
    currentMask = {
      metrics: ar.metrics,
      finalPreviewUrl: `/runs/${pid}/masks/final_mask_preview.png`,
      maskPreviewUrl: `/runs/${pid}/masks/mask_preview.png`,
      maskUrls: Object.fromEntries(
        Object.values(maskGroups).flat()
          .filter((n) => !["final_mask_preview.png", "mask_preview.png"].includes(n))
          .map((n) => [n, `/runs/${pid}/masks/${n}`])
      ),
    };
    applyProjectToImportUI(currentProject);
    $("maskPreview").src = `${currentMask.finalPreviewUrl}?t=${Date.now()}`;
    updateMaskMetrics(ar.metrics);
    renderMaskToggles(currentMask);
    applyScoreResult(ar.score);
    loadOriginalHistory();
    goStep("score");
    setStatus("自动评审完成");
    log("自动评审完成");
  } finally {
    setBusy(false);
  }
}

$("autoReviewButton").addEventListener("click", (e) =>
  withBusyButton(e.currentTarget, "自动评审中…", () =>
    autoReview().catch((e) => { setStatus(e.message, true); setBusy(false); })));

/* ------------------------------------------------------------ 深浅主题 */

const THEME_KEY = "baize-theme";

function applyTheme(value) {
  const dark = value === "dark";
  document.body.classList.toggle("dark", dark);
  const btn = $("themeToggle");
  if (btn) btn.textContent = dark ? "浅色" : "深色";
  try { localStorage.setItem(THEME_KEY, dark ? "dark" : "light"); } catch (e) { /* 隐私模式忽略 */ }
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (e) { /* 忽略 */ }
  applyTheme(saved === "dark" ? "dark" : "light");
  const btn = $("themeToggle");
  if (btn) {
    btn.addEventListener("click", () =>
      applyTheme(document.body.classList.contains("dark") ? "light" : "dark"));
  }
}

/* ------------------------------------------------------------ 启动 */

initTheme();
loadOriginalHistory();
