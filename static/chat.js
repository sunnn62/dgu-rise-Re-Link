// 실종아동 발견 신고 채팅 클라이언트.
// 순수 WebSocket으로 서버와 통신하고, 끊기면 최대 3회 자동 재연결한다.

(function () {
  "use strict";

  const config = window.CHAT_CONFIG || {};
  const messagesEl = document.getElementById("messages");
  const formEl = document.getElementById("chatForm");
  const inputEl = document.getElementById("msgInput");
  const sendEl = formEl.querySelector(".btn-send");
  const statusEl = document.getElementById("connStatus");

  const MAX_RECONNECT = 3; // 재연결 최대 시도 횟수
  let reconnectCount = 0;
  let ws = null;
  let manualClose = false;

  // ws_url이 절대경로가 아니면 현재 호스트 기준 절대 URL로 변환한다.
  function resolveWsUrl() {
    const raw = config.wsUrl || "/ws/chat/" + config.roomId + "?role=" + config.role;
    if (raw.startsWith("ws://") || raw.startsWith("wss://")) {
      return raw;
    }
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return scheme + "//" + window.location.host + raw;
  }

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = "conn-status " + cls;
  }

  // HTML 이스케이프: 사용자 입력을 그대로 DOM에 넣지 않도록 방어(XSS 방지).
  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // 첫 메시지가 렌더링되면 빈 상태 안내를 제거한다.
  function hideEmptyState() {
    const emptyState = document.getElementById("chatEmptyState");
    if (emptyState) emptyState.remove();
  }

  // 시스템 안내 메시지(가운데 정렬).
  function renderSystem(content) {
    hideEmptyState();
    const el = document.createElement("div");
    el.className = "msg-system";
    el.textContent = content;
    messagesEl.appendChild(el);
    scrollToBottom();
  }

  // 작은 원형 아바타 아이콘(상대방 말풍선 옆에 고정 표시).
  function createMiniAvatar() {
    const avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    avatar.innerHTML =
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>';
    return avatar;
  }

  // 일반 텍스트 말풍선. 내 메시지는 오른쪽, 상대는 왼쪽.
  function renderText(msg) {
    hideEmptyState();
    const mine = msg.sender_role === config.role;
    const row = document.createElement("div");
    row.className = "msg-row " + (mine ? "mine" : "theirs");

    const bubbleGroup = document.createElement("div");
    bubbleGroup.className = "bubble-group";

    if (!mine) {
      bubbleGroup.appendChild(createMiniAvatar());
    }

    const bubbleCol = document.createElement("div");
    bubbleCol.className = "bubble-col";

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    const who = document.createElement("div");
    who.className = "sender-label";
    who.textContent = msg.sender_role === "guardian" ? "보호자" : "발견자";
    bubbleCol.appendChild(who);

    const body = document.createElement("div");
    body.className = "bubble-text";
    body.innerHTML = escapeHtml(msg.content);
    bubble.appendChild(body);

    bubbleCol.appendChild(bubble);
    bubbleGroup.appendChild(bubbleCol);
    row.appendChild(bubbleGroup);
    messagesEl.appendChild(row);
    scrollToBottom();
  }

  // 위치 메시지 말풍선. 지도 링크(구글 지도)를 새 탭으로 열 수 있게 렌더링한다.
  function renderLocation(msg) {
    hideEmptyState();
    const mine = msg.sender_role === config.role;
    const row = document.createElement("div");
    row.className = "msg-row " + (mine ? "mine" : "theirs");

    const bubbleCol = document.createElement("div");
    bubbleCol.className = "bubble-col";

    const who = document.createElement("div");
    who.className = "sender-label";
    who.textContent = msg.sender_role === "guardian" ? "보호자" : "발견자";
    bubbleCol.appendChild(who);

    const lat = Number(msg.latitude);
    const lng = Number(msg.longitude);
    const mapUrl =
      "https://www.google.com/maps/search/?api=1&query=" + lat + "," + lng;

    const link = document.createElement("a");
    link.className = "location-bubble";
    link.href = mapUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.innerHTML =
      '<span class="location-bubble-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0z"/><circle cx="12" cy="10" r="3"/></svg></span>' +
      '<span class="location-bubble-copy"><strong>공유된 위치</strong><span>지도에서 보기</span></span>';

    bubbleCol.appendChild(link);
    row.appendChild(bubbleCol);
    messagesEl.appendChild(row);
    scrollToBottom();
  }

  function renderMessage(msg) {
    if (!msg || !msg.type) return;
    if (msg.type === "system") {
      renderSystem(msg.content);
    } else if (msg.type === "text") {
      renderText(msg);
    } else if (msg.type === "location") {
      renderLocation(msg);
    }
  }

  function handleServerMessage(data) {
    if (data.type === "history") {
      // 재접속 시 이전 대화 이력을 순서대로 렌더링.
      (data.messages || []).forEach(renderMessage);
      return;
    }
    renderMessage(data);
  }

  function connect() {
    setStatus("연결 중…", "conn-connecting");
    try {
      ws = new WebSocket(resolveWsUrl());
    } catch (err) {
      setStatus("연결 실패", "conn-error");
      scheduleReconnect();
      return;
    }

    ws.onopen = function () {
      reconnectCount = 0;
      setStatus("연결됨", "conn-ok");
      inputEl.disabled = false;
      // [GAP B] finder는 위치를 공유하기 전까지 이미 hideChatInput()으로
      // 입력 폼 자체가 숨겨져 있으므로 disabled 해제와 무관하게 입력 불가능하다.
    };

    ws.onmessage = function (event) {
      try {
        const data = JSON.parse(event.data);
        handleServerMessage(data);
      } catch (err) {
        // 서버가 보낸 데이터가 JSON이 아니면 무시(정상 상황에선 발생하지 않음).
        console.error("메시지 파싱 실패:", err);
      }
    };

    ws.onclose = function () {
      inputEl.disabled = true;
      if (manualClose) return;
      scheduleReconnect();
    };

    ws.onerror = function () {
      // onerror 직후 onclose가 이어지므로 여기서는 상태 표시만 갱신.
      setStatus("연결 오류", "conn-error");
    };
  }

  function scheduleReconnect() {
    if (reconnectCount >= MAX_RECONNECT) {
      setStatus("연결이 끊겼습니다. 새로고침 해주세요", "conn-error");
      renderSystem("연결이 끊겼습니다. 새로고침 해주세요.");
      return;
    }
    reconnectCount += 1;
    setStatus("재연결 시도 " + reconnectCount + "/" + MAX_RECONNECT + "…", "conn-connecting");
    // 점진적 지연(1s, 2s, 3s) 후 재시도.
    setTimeout(connect, reconnectCount * 1000);
  }

  function syncSendButton() {
    if (sendEl) sendEl.disabled = !inputEl.value.trim();
  }

  inputEl.addEventListener("input", syncSendButton);
  inputEl.addEventListener("keydown", function (event) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.isComposing &&
      event.keyCode !== 229
    ) {
      event.preventDefault();
      formEl.requestSubmit();
    }
  });
  syncSendButton();

  formEl.addEventListener("submit", function (e) {
    e.preventDefault();
    const text = inputEl.value.trim();
    if (!text) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      renderSystem("아직 연결되지 않았습니다. 잠시만 기다려주세요.");
      return;
    }
    ws.send(JSON.stringify({ type: "text", content: text }));
    inputEl.value = "";
    syncSendButton();
    inputEl.focus();
  });

  // ---------- [GAP B] 위치-잠금 게이트 + 112 신고 경로 (발견자 전용) ----------
  const locationGate = document.getElementById("locationGate");
  const emergencyPanel = document.getElementById("emergencyPanel");
  const shareLocationBtn = document.getElementById("shareLocationBtn");
  const emergency112Btn = document.getElementById("emergency112Btn");
  const backToLocationGateBtn = document.getElementById("backToLocationGateBtn");

  let locationShared = false;

  function showChatInput() {
    formEl.hidden = false;
  }

  function hideChatInput() {
    formEl.hidden = true;
  }

  function showLocationGate() {
    if (locationGate) locationGate.hidden = false;
    if (emergencyPanel) emergencyPanel.hidden = true;
    hideChatInput();
  }

  function showEmergencyPanel() {
    if (locationGate) locationGate.hidden = true;
    if (emergencyPanel) emergencyPanel.hidden = false;
    hideChatInput();
  }

  function unlockChatAfterLocation() {
    locationShared = true;
    if (locationGate) locationGate.hidden = true;
    if (emergencyPanel) emergencyPanel.hidden = true;
    showChatInput();
  }

  if (config.role === "guardian") {
    // 보호자는 위치-잠금이 적용되지 않는다. 연결되면 곧바로 입력창을 연다.
    showChatInput();
  } else {
    // 발견자는 위치를 공유하기 전까지 채팅 입력창을 볼 수 없다.
    showLocationGate();
  }

  function requestAndShareLocation() {
    if (!("geolocation" in navigator)) {
      renderSystem("이 브라우저는 위치 기능을 지원하지 않습니다.");
      return;
    }
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      renderSystem("연결된 후에 위치를 공유할 수 있습니다.");
      return;
    }

    shareLocationBtn.disabled = true;
    shareLocationBtn.textContent = "위치 확인 중…";

    navigator.geolocation.getCurrentPosition(
      function (position) {
        ws.send(
          JSON.stringify({
            type: "location",
            latitude: position.coords.latitude,
            longitude: position.coords.longitude,
          })
        );
        shareLocationBtn.disabled = false;
        shareLocationBtn.textContent = "위치 공유하고 채팅 시작";
        unlockChatAfterLocation();
      },
      function (error) {
        let reason = "위치를 가져오지 못했습니다.";
        if (error.code === error.PERMISSION_DENIED) {
          reason = "위치 권한이 거부되었습니다. 브라우저 설정에서 허용해주세요.";
        } else if (error.code === error.TIMEOUT) {
          reason = "위치 확인 시간이 초과되었습니다. 다시 시도해주세요.";
        }
        renderSystem(reason);
        shareLocationBtn.disabled = false;
        shareLocationBtn.textContent = "위치 공유하고 채팅 시작";
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
    );
  }

  if (shareLocationBtn) {
    shareLocationBtn.addEventListener("click", requestAndShareLocation);
  }

  if (emergency112Btn) {
    emergency112Btn.addEventListener("click", function () {
      // [알려진 갭] 112 경로는 현재 클라이언트 UI 안내만 제공한다. 백엔드에 별도
      // 신고 처리 API/메시지 타입이 없으므로 서버에는 아무것도 전송하지 않는다.
      // (PLAN.md "112 신고 대체 경로" — 서버 쪽 시스템 메시지 통지는 추후 백엔드 작업)
      showEmergencyPanel();
    });
  }

  if (backToLocationGateBtn) {
    backToLocationGateBtn.addEventListener("click", function () {
      showLocationGate();
    });
  }

  // 페이지를 떠날 때는 재연결하지 않도록 표시.
  window.addEventListener("beforeunload", function () {
    manualClose = true;
    if (ws) ws.close();
  });

  connect();
})();
