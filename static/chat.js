// 실종아동 발견 신고 채팅 클라이언트.
// 순수 WebSocket으로 서버와 통신하고, 끊기면 최대 3회 자동 재연결한다.

(function () {
  "use strict";

  const config = window.CHAT_CONFIG || {};
  const messagesEl = document.getElementById("messages");
  const formEl = document.getElementById("chatForm");
  const inputEl = document.getElementById("msgInput");
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

  // 시스템 안내 메시지(가운데 정렬).
  function renderSystem(content) {
    const el = document.createElement("div");
    el.className = "msg-system";
    el.textContent = content;
    messagesEl.appendChild(el);
    scrollToBottom();
  }

  // 일반 텍스트 말풍선. 내 메시지는 오른쪽, 상대는 왼쪽.
  function renderText(msg) {
    const mine = msg.sender_role === config.role;
    const row = document.createElement("div");
    row.className = "msg-row " + (mine ? "mine" : "theirs");

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    if (!mine) {
      const who = document.createElement("div");
      who.className = "sender-label";
      who.textContent = msg.sender_role === "guardian" ? "보호자" : "발견자";
      bubble.appendChild(who);
    }

    const body = document.createElement("div");
    body.className = "bubble-text";
    body.innerHTML = escapeHtml(msg.content);
    bubble.appendChild(body);

    row.appendChild(bubble);
    messagesEl.appendChild(row);
    scrollToBottom();
  }

  // [Week2] 오차 반경(미터)이 이 값을 넘으면 "믿을 수 없는 위치"로 간주하고
  // 발견자에게 재시도를 안내한다(PLAN GAP B "위치 오차 미표시" 개선 항목).
  const ACCURACY_WARN_THRESHOLD_METERS = 100;

  // 위치 메시지 말풍선. 지도 링크(구글 지도)를 새 탭으로 열 수 있게 렌더링한다.
  // [Week2] 오차 반경(accuracy)도 함께 표시해, 보호자가 이 위치를 얼마나
  // 믿어야 하는지 판단할 수 있게 한다.
  function renderLocation(msg) {
    const mine = msg.sender_role === config.role;
    const row = document.createElement("div");
    row.className = "msg-row " + (mine ? "mine" : "theirs");

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    if (!mine) {
      const who = document.createElement("div");
      who.className = "sender-label";
      who.textContent = msg.sender_role === "guardian" ? "보호자" : "발견자";
      bubble.appendChild(who);
    }

    const lat = Number(msg.latitude);
    const lng = Number(msg.longitude);
    const mapUrl = "https://www.google.com/maps?q=" + lat + "," + lng;

    const link = document.createElement("a");
    link.className = "location-link";
    link.href = mapUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "📍 공유된 위치 보기 (지도 열기)";
    bubble.appendChild(link);

    const coords = document.createElement("div");
    coords.className = "location-coords";
    coords.textContent = lat.toFixed(5) + ", " + lng.toFixed(5);
    bubble.appendChild(coords);

    // accuracy가 있으면 오차 반경을 함께 표시한다(없으면 생략, 구버전 호환).
    const accuracy = msg.accuracy != null ? Number(msg.accuracy) : null;
    if (accuracy != null && !Number.isNaN(accuracy)) {
      const accuracyEl = document.createElement("div");
      const isImprecise = accuracy > ACCURACY_WARN_THRESHOLD_METERS;
      accuracyEl.className = "location-accuracy" + (isImprecise ? " is-imprecise" : "");
      accuracyEl.textContent =
        (isImprecise ? "⚠️ " : "") + "오차 반경 약 " + Math.round(accuracy) + "m";
      bubble.appendChild(accuracyEl);

      // 정확도가 낮은 위치를 "내가" 보낸 경우, 재시도를 안내한다.
      if (isImprecise && mine) {
        const retryEl = document.createElement("div");
        retryEl.className = "location-accuracy-hint";
        retryEl.textContent = "실외로 이동해서 다시 시도하면 더 정확한 위치를 보낼 수 있어요.";
        bubble.appendChild(retryEl);
      }
    }

    row.appendChild(bubble);
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
    if (data.type === "gate") {
      // [Week2] 서버가 알려주는 "이 발견자의" 위치 공유 상태. 발견자마다 각자
      // 위치를 공유해야 하므로(다른 발견자가 공유했어도 내 잠금은 안 풀림),
      // 게이트를 보여줄지/입력창을 바로 열지는 항상 서버 판정을 따른다.
      // 이미 공유한 발견자가 새로고침해도 게이트가 다시 뜨지 않게 해준다.
      if (config.role === "finder") {
        locationAlreadyShared = !!data.location_shared;
        if (locationAlreadyShared) {
          showChatInput();
        } else {
          showLocationGate();
        }
      }
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
    inputEl.focus();
  });

  // ---------- [Week2/GAP B] 위치 공유 게이트 + 112 대체 경로 (발견자 전용) ----------
  const locationGate = document.getElementById("locationGate");
  const shareLocationBtn = document.getElementById("shareLocationBtn");
  const declineLocationBtn = document.getElementById("declineLocationBtn");
  const emergencyNotice = document.getElementById("emergencyNotice");

  // 서버 이력(history)의 location 메시지 존재 여부로 "이미 위치를 공유했는지"를
  // 판단한다. finder 텍스트 게이트는 서버(main.py _receive_loop)가 최종 판단하며,
  // 이 UI는 사용자 경험을 위한 보조 표시일 뿐이다(서버 재검증이 진짜 방어선).
  let locationAlreadyShared = false;

  function showChatInput() {
    formEl.hidden = false;
    inputEl.disabled = false;
    if (locationGate) locationGate.hidden = true;
    if (emergencyNotice) emergencyNotice.hidden = true;
  }

  function showLocationGate() {
    if (locationGate) locationGate.hidden = false;
    formEl.hidden = true;
    if (emergencyNotice) emergencyNotice.hidden = true;
  }

  function showEmergencyNotice() {
    if (emergencyNotice) emergencyNotice.hidden = false;
    formEl.hidden = true;
    if (locationGate) locationGate.hidden = true;
  }

  // 발견자는 위치 공유 전까지 채팅 입력창 대신 위치 공유 게이트를 본다.
  if (config.role === "finder") {
    formEl.hidden = true; // 기본값: 게이트를 먼저 보여준다(연결 후 상태에 따라 갱신).
  }

  if (shareLocationBtn) {
    shareLocationBtn.addEventListener("click", function () {
      if (!("geolocation" in navigator)) {
        renderSystem("이 브라우저는 위치 기능을 지원하지 않습니다.");
        return;
      }
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        renderSystem("연결된 후에 위치를 공유할 수 있습니다.");
        return;
      }

      shareLocationBtn.disabled = true;
      shareLocationBtn.textContent = "📍 위치 확인 중…";

      navigator.geolocation.getCurrentPosition(
        function (position) {
          // [Week2] 오차 반경(accuracy, 미터)도 함께 전송한다. 서버가 저장하고
          // 브로드캐스트하는 값이며, 화면 표시는 renderLocation이 담당한다.
          ws.send(
            JSON.stringify({
              type: "location",
              latitude: position.coords.latitude,
              longitude: position.coords.longitude,
              accuracy: position.coords.accuracy,
            })
          );
          shareLocationBtn.disabled = false;
          shareLocationBtn.textContent = "📍 위치 공유하고 채팅 시작";
          locationAlreadyShared = true;
          showChatInput();

          // 오차가 크면(실내/도심 등) 채팅을 연 상태에서 재시도를 권장한다.
          // 위치 자체는 이미 서버에 전달됐으므로 채팅을 막지는 않는다.
          if (
            typeof position.coords.accuracy === "number" &&
            position.coords.accuracy > ACCURACY_WARN_THRESHOLD_METERS
          ) {
            renderSystem(
              "위치 정확도가 낮습니다(오차 약 " +
                Math.round(position.coords.accuracy) +
                "m). 실외로 이동해서 위치를 다시 공유해보세요."
            );
          }
        },
        function (error) {
          // 권한 거부/시간 초과 등. 사용자에게 원인을 안내한다.
          let reason = "위치를 가져오지 못했습니다.";
          if (error.code === error.PERMISSION_DENIED) {
            reason = "위치 권한이 거부되었습니다. 브라우저 설정에서 허용해주세요.";
          } else if (error.code === error.TIMEOUT) {
            reason = "위치 확인 시간이 초과되었습니다. 다시 시도해주세요.";
          }
          renderSystem(reason);
          shareLocationBtn.disabled = false;
          shareLocationBtn.textContent = "📍 위치 공유하고 채팅 시작";
        },
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
      );
    });
  }

  // [Week2/GAP B] 위치 공유 없이 112 신고 경로로 넘어가는 버튼.
  // tel: 링크로 실제 전화 연결을 시도하는 동시에, 서버에 이 선택을 알려
  // 보호자에게 시스템 메시지("발견자가 위치 공유 없이 112 신고 경로로
  // 안내받았습니다")가 전달되게 한다. 채팅 입력창은 열리지 않는다.
  if (declineLocationBtn) {
    declineLocationBtn.addEventListener("click", function () {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "decline_location" }));
      }
      showEmergencyNotice();
      // tel: 링크의 기본 동작(전화 앱 열기)은 막지 않고 그대로 진행시킨다.
    });
  }

  // 페이지를 떠날 때는 재연결하지 않도록 표시.
  window.addEventListener("beforeunload", function () {
    manualClose = true;
    if (ws) ws.close();
  });

  connect();
})();
