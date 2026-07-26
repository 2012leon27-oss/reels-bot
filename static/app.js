(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const elements = {
    hero: $("#hero"),
    workspace: $("#workspace"),
    source: $("#source-input"),
    count: $("#char-count"),
    mode: $("#mode-select"),
    record: $("#record-button"),
    recordLabel: $("#record-label"),
    recordTime: $("#record-time"),
    audioUpload: $("#audio-upload"),
    structure: $("#structure-button"),
    result: $("#result"),
    resultContent: $("#result-content"),
    original: $("#original-content"),
    loading: $("#loading"),
    loadingTitle: $("#loading-title"),
    loadingCopy: $("#loading-copy"),
    copy: $("#copy-button"),
    download: $("#download-button"),
    share: $("#share-button"),
    revoke: $("#revoke-button"),
    sharedNote: $("#shared-note"),
    sharedContent: $("#shared-content"),
    sharedOriginal: $("#shared-original"),
    historyButton: $("#history-button"),
    historyPanel: $("#history-panel"),
    historyClose: $("#history-close"),
    historyList: $("#history-list"),
    backdrop: $("#panel-backdrop"),
    accessButton: $("#access-button"),
    accessDialog: $("#access-dialog"),
    accessForm: $("#access-form"),
    accessInput: $("#access-key-input"),
    toast: $("#toast"),
  };

  const state = {
    note: null,
    recorder: null,
    stream: null,
    chunks: [],
    startedAt: 0,
    timer: null,
    toastTimer: null,
    pendingAudio: null,
    retryAction: null,
    recordedBytes: 0,
    historyReturnFocus: null,
  };

  const telegram = window.Telegram?.WebApp;
  if (telegram) {
    telegram.ready();
    telegram.expand();
    telegram.setHeaderColor?.("#f8f6ef");
    telegram.setBackgroundColor?.("#f4f1e9");
  }

  const authHeaders = () => {
    const headers = {};
    if (telegram?.initData) headers["X-Telegram-Init-Data"] = telegram.initData;
    const key = sessionStorage.getItem("thought-architect-access-key");
    if (key) headers["X-App-Access-Key"] = key;
    return headers;
  };

  async function api(path, options = {}) {
    const isForm = options.body instanceof FormData;
    const response = await fetch(path, {
      ...options,
      headers: {
        ...authHeaders(),
        ...(isForm ? {} : { "Content-Type": "application/json" }),
        ...(options.headers || {}),
      },
    });
    if (response.status === 204) return null;
    let body;
    try {
      body = await response.json();
    } catch {
      body = {};
    }
    if (!response.ok) {
      if (response.status === 401) openAccessDialog();
      const error = new Error(body.error || `Ошибка ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function showToast(message) {
    clearTimeout(state.toastTimer);
    elements.toast.textContent = message;
    elements.toast.hidden = false;
    state.toastTimer = setTimeout(() => {
      elements.toast.hidden = true;
    }, 3600);
  }

  function setLoading(active, title = "Собираю мысль", copy = "Сохраняю контекст и отделяю решения от идей…") {
    elements.loadingTitle.textContent = title;
    elements.loadingCopy.textContent = copy;
    elements.loading.hidden = !active;
    elements.structure.disabled = active || elements.source.value.length > 50000;
    elements.record.disabled = active;
    elements.audioUpload.disabled = active;
    elements.source.readOnly = active;
    elements.mode.disabled = active;
    if (active) {
      elements.loading.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  function inlineContent(text) {
    const fragment = document.createDocumentFragment();
    const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    let cursor = 0;
    for (const match of text.matchAll(pattern)) {
      fragment.append(document.createTextNode(text.slice(cursor, match.index)));
      const token = match[0];
      const node = token.startsWith("**")
        ? document.createElement("strong")
        : document.createElement("code");
      node.textContent = token.startsWith("**") ? token.slice(2, -2) : token.slice(1, -1);
      fragment.append(node);
      cursor = match.index + token.length;
    }
    fragment.append(document.createTextNode(text.slice(cursor)));
    return fragment;
  }

  function appendMarkdown(container, markdown, titleToSkip = "") {
    let currentList = null;
    let currentType = "";
    const finishList = () => {
      currentList = null;
      currentType = "";
    };

    for (const rawLine of String(markdown || "").split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line) {
        finishList();
        continue;
      }
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        finishList();
        if (
          heading[1].length === 1 &&
          heading[2].trim().toLowerCase() === titleToSkip.trim().toLowerCase()
        ) {
          continue;
        }
        const node = document.createElement(`h${Math.min(heading[1].length + 1, 3)}`);
        node.append(inlineContent(heading[2]));
        container.append(node);
        continue;
      }

      const unordered = line.match(/^[-*]\s+(.+)$/);
      const ordered = line.match(/^\d+[.)]\s+(.+)$/);
      if (unordered || ordered) {
        const type = ordered ? "ol" : "ul";
        if (!currentList || currentType !== type) {
          currentList = document.createElement(type);
          currentType = type;
          container.append(currentList);
        }
        const item = document.createElement("li");
        item.append(inlineContent((unordered || ordered)[1]));
        currentList.append(item);
        continue;
      }

      finishList();
      const paragraph = document.createElement("p");
      paragraph.append(inlineContent(line));
      container.append(paragraph);
    }
  }

  function factBlock(title, values) {
    if (!values?.length) return null;
    const block = document.createElement("section");
    block.className = "fact-block";
    const heading = document.createElement("h3");
    heading.textContent = title;
    const list = document.createElement("ul");
    for (const value of values) {
      const item = document.createElement("li");
      item.textContent = value;
      list.append(item);
    }
    block.append(heading, list);
    return block;
  }

  function actionText(action) {
    const metadata = [
      action.owner ? `ответственный: ${action.owner}` : "",
      action.deadline ? `срок: ${action.deadline}` : "",
    ].filter(Boolean);
    return metadata.length ? `${action.task} — ${metadata.join(", ")}` : action.task;
  }

  function renderDocument(container, note) {
    container.replaceChildren();
    const structured = note.structured || {};
    const title = document.createElement("h1");
    title.textContent = structured.title || note.title || "Заметка";
    container.append(title);

    if (structured.summary) {
      const summary = document.createElement("p");
      summary.className = "summary";
      summary.textContent = structured.summary;
      container.append(summary);
    }
    appendMarkdown(container, structured.structured_text, structured.title);

    const blocks = [
      factBlock("Решения", structured.decisions),
      factBlock("Следующие действия", (structured.actions || []).map(actionText)),
      factBlock("Идеи и гипотезы", structured.ideas),
      factBlock("Открытые вопросы", structured.open_questions),
    ].filter(Boolean);
    if (blocks.length) {
      const grid = document.createElement("div");
      grid.className = "fact-grid";
      grid.append(...blocks);
      container.append(grid);
    }

    if (structured.tags?.length) {
      const tags = document.createElement("div");
      tags.className = "tags";
      for (const value of structured.tags) {
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = value;
        tags.append(tag);
      }
      container.append(tags);
    }
  }

  function renderResult(note) {
    state.note = note;
    elements.share.textContent = note.share_enabled ? "Поделиться" : "Создать ссылку";
    elements.revoke.hidden = !note.share_enabled;
    renderDocument(elements.resultContent, note);
    elements.original.textContent = note.source_text || "";
    elements.result.hidden = false;
    elements.result.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function structureCurrentText() {
    const text = elements.source.value.trim();
    if (!text) {
      showToast("Сначала добавьте текст или голосовую запись.");
      elements.source.focus();
      return;
    }
    setLoading(true);
    state.retryAction = structureCurrentText;
    try {
      const note = await api("/api/structure", {
        method: "POST",
        body: JSON.stringify({ text, mode: elements.mode.value }),
      });
      state.retryAction = null;
      localStorage.removeItem("thought-architect-draft");
      renderResult(note);
    } catch (error) {
      if (error.status !== 401) state.retryAction = null;
      showToast(error.message);
    } finally {
      setLoading(false);
    }
  }

  async function transcribeFile(file) {
    if (!file) return;
    const maxBytes = 25 * 1024 * 1024;
    if (file.size > maxBytes) {
      showToast("Файл больше 25 МБ. Сожмите запись и попробуйте снова.");
      return;
    }
    state.pendingAudio = file;
    state.retryAction = () => transcribeFile(state.pendingAudio);
    setLoading(true, "Слушаю запись", "Распознаю речь. После этого текст можно будет проверить и дополнить.");
    try {
      const data = new FormData();
      data.append("audio", file, file.name || "recording.webm");
      const response = await api("/api/transcribe", { method: "POST", body: data });
      const combined = [elements.source.value.trim(), response.text]
        .filter(Boolean)
        .join("\n\n");
      elements.source.value = combined;
      state.pendingAudio = null;
      state.retryAction = null;
      elements.recordLabel.textContent = "Записать голос";
      updateCount();
      saveDraft();
      elements.source.focus();
      showToast(
        combined.length > 50000
          ? "Транскрипт сохранён, но длиннее 50 000 символов. Разделите его перед обработкой."
          : "Расшифровка готова. Проверьте текст и соберите структуру."
      );
      elements.workspace.scrollIntoView({ behavior: "smooth", block: "center" });
    } catch (error) {
      if (error.status !== 401) state.retryAction = () => transcribeFile(state.pendingAudio);
      elements.recordLabel.textContent = "Повторить расшифровку";
      showToast(error.message);
    } finally {
      setLoading(false);
      elements.audioUpload.value = "";
    }
  }

  function supportedMimeType() {
    const types = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
    return types.find((type) => window.MediaRecorder?.isTypeSupported(type)) || "";
  }

  async function startRecording() {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      showToast("Браузер не поддерживает запись. Используйте кнопку «Аудиофайл».");
      return;
    }
    try {
      state.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = supportedMimeType();
      state.recorder = new MediaRecorder(state.stream, mimeType ? { mimeType } : undefined);
      state.chunks = [];
      state.recordedBytes = 0;
      state.recorder.addEventListener("dataavailable", (event) => {
        if (event.data.size) {
          state.chunks.push(event.data);
          state.recordedBytes += event.data.size;
          if (state.recordedBytes > 24 * 1024 * 1024 && state.recorder.state === "recording") {
            showToast("Запись достигла лимита 24 МБ и остановлена.");
            stopRecording();
          }
        }
      });
      state.recorder.addEventListener("stop", () => {
        const type = state.recorder.mimeType || "audio/webm";
        const extension = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
        const blob = new Blob(state.chunks, { type });
        transcribeFile(new File([blob], `voice-${Date.now()}.${extension}`, { type }));
      });
      state.recorder.addEventListener("error", () => {
        state.stream?.getTracks().forEach((track) => track.stop());
        clearInterval(state.timer);
        elements.record.classList.remove("recording");
        elements.recordLabel.textContent = "Записать голос";
        elements.recordTime.hidden = true;
        elements.structure.disabled = false;
        elements.audioUpload.disabled = false;
        showToast("Запись прервалась. Попробуйте ещё раз.");
      });
      state.recorder.start(1000);
      state.startedAt = Date.now();
      elements.record.classList.add("recording");
      elements.recordLabel.textContent = "Остановить";
      elements.recordTime.hidden = false;
      elements.structure.disabled = true;
      elements.audioUpload.disabled = true;
      state.timer = setInterval(updateRecordingTime, 250);
      updateRecordingTime();
    } catch (error) {
      state.stream?.getTracks().forEach((track) => track.stop());
      elements.structure.disabled = false;
      elements.audioUpload.disabled = false;
      showToast(error.name === "NotAllowedError"
        ? "Разрешите доступ к микрофону в настройках браузера."
        : "Не удалось начать запись. Используйте загрузку аудиофайла.");
    }
  }

  function stopRecording() {
    if (state.recorder?.state === "recording") state.recorder.stop();
    state.stream?.getTracks().forEach((track) => track.stop());
    clearInterval(state.timer);
    elements.record.classList.remove("recording");
    elements.recordLabel.textContent = "Записать голос";
    elements.recordTime.hidden = true;
    elements.structure.disabled = false;
    elements.audioUpload.disabled = false;
  }

  function updateRecordingTime() {
    const seconds = Math.floor((Date.now() - state.startedAt) / 1000);
    const minutes = String(Math.floor(seconds / 60)).padStart(2, "0");
    const remainder = String(seconds % 60).padStart(2, "0");
    elements.recordTime.textContent = `${minutes}:${remainder}`;
    if (seconds >= 30 * 60 && state.recorder?.state === "recording") {
      showToast("Запись достигла лимита 30 минут и остановлена.");
      stopRecording();
    }
  }

  function updateCount() {
    elements.count.textContent = `${elements.source.value.length.toLocaleString("ru-RU")} / 50 000`;
    elements.count.classList.toggle("over-limit", elements.source.value.length > 50000);
    elements.structure.disabled = elements.source.value.length > 50000;
  }

  let draftTimer;
  function saveDraft() {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => {
      if (elements.source.value) {
        localStorage.setItem("thought-architect-draft", elements.source.value);
      } else {
        localStorage.removeItem("thought-architect-draft");
      }
    }, 300);
  }

  async function copyText(text, confirmation = "Скопировано") {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const input = document.createElement("textarea");
        input.value = text;
        input.style.position = "fixed";
        input.style.opacity = "0";
        document.body.append(input);
        input.select();
        if (!document.execCommand("copy")) throw new Error("copy failed");
        input.remove();
      }
      showToast(confirmation);
    } catch {
      showToast("Браузер не разрешил копирование.");
    }
  }

  function safeFilename(title) {
    const value = String(title || "note")
      .toLowerCase()
      .replace(/[^\p{L}\p{N}]+/gu, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 70);
    return value || "note";
  }

  async function shareNote() {
    if (!state.note) return;
    if (!state.note.share_enabled) {
      const includeSource = window.confirm(
        "Добавить в публичную страницу оригинальную запись?\n\n" +
        "«Отмена» создаст ссылку только на структурированный результат."
      );
      try {
        state.note = await api(`/api/notes/${state.note.id}/share`, {
          method: "POST",
          body: JSON.stringify({ enabled: true, include_source: includeSource }),
        });
        elements.share.textContent = "Поделиться";
        elements.revoke.hidden = false;
        showToast("Ссылка создана. Доступ получит любой, у кого она есть.");
      } catch (error) {
        showToast(error.message);
        return;
      }
    }
    const data = {
      title: state.note.title,
      text: state.note.structured?.summary || state.note.title,
      url: state.note.share_url,
    };
    if (navigator.share) {
      try {
        await navigator.share(data);
        return;
      } catch (error) {
        if (error.name === "AbortError") return;
      }
    }
    await copyText(state.note.share_url, "Ссылка на заметку скопирована");
  }

  async function revokeShare() {
    if (!state.note?.share_enabled) return;
    try {
      state.note = await api(`/api/notes/${state.note.id}/share`, {
        method: "POST",
        body: JSON.stringify({ enabled: false, include_source: false }),
      });
      elements.share.textContent = "Создать ссылку";
      elements.revoke.hidden = true;
      showToast("Публичная ссылка закрыта.");
    } catch (error) {
      showToast(error.message);
    }
  }

  function downloadNote() {
    if (!state.note) return;
    const blob = new Blob([state.note.structured_markdown], { type: "text/markdown;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${safeFilename(state.note.title)}.md`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 500);
  }

  function openHistory() {
    state.historyReturnFocus = document.activeElement;
    elements.historyPanel.inert = false;
    elements.historyPanel.classList.add("open");
    elements.historyPanel.setAttribute("aria-hidden", "false");
    elements.backdrop.hidden = false;
    elements.historyClose.focus();
    loadHistory();
  }

  function closeHistory() {
    elements.historyPanel.classList.remove("open");
    elements.historyPanel.setAttribute("aria-hidden", "true");
    elements.historyPanel.inert = true;
    elements.backdrop.hidden = true;
    state.historyReturnFocus?.focus?.();
  }

  async function loadHistory() {
    elements.historyList.innerHTML = '<div class="history-empty">Загружаю архив…</div>';
    state.retryAction = loadHistory;
    try {
      const { notes } = await api("/api/notes");
      state.retryAction = null;
      elements.historyList.replaceChildren();
      if (!notes.length) {
        elements.historyList.innerHTML =
          '<div class="history-empty">Здесь появятся структурированные заметки.</div>';
        return;
      }
      for (const note of notes) {
        const link = document.createElement("a");
        link.className = "history-item";
        link.href = note.share_url;
        link.addEventListener("click", async (event) => {
          event.preventDefault();
          try {
            const fullNote = await api(`/api/notes/${note.id}`);
            closeHistory();
            renderResult(fullNote);
          } catch (error) {
            showToast(error.message);
          }
        });
        const title = document.createElement("strong");
        title.textContent = note.title;
        const date = document.createElement("small");
        date.textContent = new Intl.DateTimeFormat("ru-RU", {
          day: "numeric",
          month: "long",
          year: "numeric",
        }).format(new Date(note.created_at));
        link.append(title, date);
        elements.historyList.append(link);
      }
    } catch (error) {
      if (error.status !== 401) state.retryAction = null;
      elements.historyList.replaceChildren();
      const message = document.createElement("div");
      message.className = "history-empty";
      message.textContent = error.message;
      elements.historyList.append(message);
    }
  }

  function openAccessDialog() {
    elements.accessInput.value = sessionStorage.getItem("thought-architect-access-key") || "";
    if (!elements.accessDialog.open) {
      if (typeof elements.accessDialog.showModal === "function") {
        elements.accessDialog.showModal();
      } else {
        elements.accessDialog.setAttribute("open", "");
        elements.accessDialog.classList.add("dialog-fallback");
      }
    }
    setTimeout(() => elements.accessInput.focus(), 50);
  }

  function closeAccessDialog() {
    if (typeof elements.accessDialog.close === "function") elements.accessDialog.close();
    else {
      elements.accessDialog.removeAttribute("open");
      elements.accessDialog.classList.remove("dialog-fallback");
    }
  }

  async function loadSharedNote(shareId) {
    elements.hero.hidden = true;
    elements.workspace.hidden = true;
    elements.historyButton.hidden = true;
    elements.accessButton.hidden = true;
    elements.loading.hidden = false;
    setLoading(true, "Открываю заметку", "Загружаю структурированный документ…");
    try {
      const note = await api(`/api/public/${encodeURIComponent(shareId)}`);
      document.title = `${note.title} — Архитектор мыслей`;
      renderDocument(elements.sharedContent, note);
      elements.sharedOriginal.textContent = note.source_text || "";
      elements.sharedOriginal.parentElement.hidden = !note.source_text;
      elements.sharedNote.hidden = false;
    } catch (error) {
      elements.sharedContent.replaceChildren();
      const title = document.createElement("h1");
      title.textContent = "Заметка недоступна";
      const copy = document.createElement("p");
      copy.className = "summary";
      copy.textContent = error.message;
      elements.sharedContent.append(title, copy);
      elements.sharedNote.hidden = false;
    } finally {
      setLoading(false);
    }
  }

  async function loadPrivateNote(noteId) {
    setLoading(true, "Открываю заметку", "Проверяю доступ и загружаю документ…");
    state.retryAction = () => loadPrivateNote(noteId);
    try {
      const note = await api(`/api/notes/${encodeURIComponent(noteId)}`);
      state.retryAction = null;
      renderResult(note);
    } catch (error) {
      if (error.status !== 401) state.retryAction = null;
      showToast(error.message);
    } finally {
      setLoading(false);
    }
  }

  elements.source.addEventListener("input", () => {
    updateCount();
    saveDraft();
  });
  elements.structure.addEventListener("click", structureCurrentText);
  elements.record.addEventListener("click", () => {
    if (state.recorder?.state === "recording") stopRecording();
    else if (state.pendingAudio) transcribeFile(state.pendingAudio);
    else startRecording();
  });
  elements.audioUpload.addEventListener("change", () => transcribeFile(elements.audioUpload.files[0]));
  elements.copy.addEventListener("click", () => {
    if (state.note) copyText(state.note.structured_markdown);
  });
  elements.download.addEventListener("click", downloadNote);
  elements.share.addEventListener("click", shareNote);
  elements.revoke.addEventListener("click", revokeShare);
  elements.historyButton.addEventListener("click", openHistory);
  elements.historyClose.addEventListener("click", closeHistory);
  elements.backdrop.addEventListener("click", closeHistory);
  elements.accessButton.addEventListener("click", openAccessDialog);
  elements.accessForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (event.submitter?.value === "cancel") {
      closeAccessDialog();
      return;
    }
    const key = elements.accessInput.value.trim();
    if (key) sessionStorage.setItem("thought-architect-access-key", key);
    else sessionStorage.removeItem("thought-architect-access-key");
    closeAccessDialog();
    showToast("Ключ доступа сохранён до закрытия вкладки.");
    const retry = state.retryAction;
    state.retryAction = null;
    if (retry) setTimeout(retry, 0);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && elements.historyPanel.classList.contains("open")) {
      closeHistory();
    }
  });
  window.addEventListener("pagehide", () => {
    if (state.recorder?.state === "recording") state.recorder.stop();
    state.stream?.getTracks().forEach((track) => track.stop());
  });

  const sharedMatch = window.location.pathname.match(/^\/s\/([^/]+)$/);
  if (sharedMatch) {
    loadSharedNote(sharedMatch[1]);
  } else {
    const draft = localStorage.getItem("thought-architect-draft");
    if (draft) {
      elements.source.value = draft;
      updateCount();
    }
    const privateNoteId = new URLSearchParams(window.location.search).get("note");
    if (privateNoteId) loadPrivateNote(privateNoteId);
  }
})();
