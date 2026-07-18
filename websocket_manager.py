"""room별 WebSocket 커넥션 관리자.

같은 room_id로 접속한 발견자·보호자 커넥션을 서버 메모리에서 매칭하고,
한쪽에서 온 메시지를 같은 방의 다른 커넥션들에게 브로드캐스트한다.

외부 라이브러리(Socket.IO 등) 없이 FastAPI 내장 WebSocket만 사용한다.
연결 상태는 프로세스 메모리에만 존재하므로(서버 재시작 시 초기화), 메시지
'이력'은 DB(Message 테이블)에 별도로 저장한다. 이 클래스는 실시간 전달만 담당.
"""

from __future__ import annotations

import asyncio

from fastapi import WebSocket


class ConnectionManager:
    """room_id별로 활성 WebSocket 커넥션을 추적하고 브로드캐스트한다."""

    def __init__(self) -> None:
        # room_id -> 해당 방에 연결된 WebSocket 목록.
        self._rooms: dict[str, list[WebSocket]] = {}
        # _rooms 동시 수정으로 인한 경합을 막기 위한 잠금.
        self._lock = asyncio.Lock()

    async def connect(self, room_id: str, websocket: WebSocket) -> None:
        """핸드셰이크를 수락하고 커넥션을 방에 등록한다."""
        await websocket.accept()
        async with self._lock:
            self._rooms.setdefault(room_id, []).append(websocket)

    async def disconnect(self, room_id: str, websocket: WebSocket) -> None:
        """커넥션을 방에서 제거한다(방이 비면 방 자체도 정리)."""
        async with self._lock:
            connections = self._rooms.get(room_id)
            if not connections:
                return
            if websocket in connections:
                connections.remove(websocket)
            if not connections:
                self._rooms.pop(room_id, None)

    async def broadcast(
        self, room_id: str, message: dict, exclude: WebSocket | None = None
    ) -> None:
        """같은 방의 모든 커넥션에 JSON 메시지를 전송한다.

        exclude로 지정한 커넥션(보통 발신자 자신)은 제외한다. 전송 도중 이미
        끊긴 커넥션이 있으면 조용히 건너뛴다(정리는 각 커넥션의 핸들러가 담당).
        """
        # 순회 중 목록이 바뀌어도 안전하도록 스냅샷을 뜬다.
        async with self._lock:
            targets = list(self._rooms.get(room_id, []))

        for connection in targets:
            if connection is exclude:
                continue
            try:
                await connection.send_json(message)
            except (RuntimeError, ConnectionError):
                # 이미 닫힌 커넥션에 전송 시도한 경우. 해당 커넥션의 수신 루프가
                # WebSocketDisconnect를 받아 스스로 정리하므로 여기서는 무시한다.
                continue

    async def room_size(self, room_id: str) -> int:
        """현재 방에 연결된 커넥션 수를 반환한다."""
        async with self._lock:
            return len(self._rooms.get(room_id, []))

    async def close_room(self, room_id: str, reason: str) -> None:
        """방의 모든 커넥션에 종료 안내를 보낸 뒤 연결을 닫는다.

        각 커넥션을 닫으면 해당 수신 루프가 WebSocketDisconnect를 받아 스스로
        정리하므로, 여기서 _rooms에서 직접 제거하지는 않는다.
        """
        async with self._lock:
            targets = list(self._rooms.get(room_id, []))

        for connection in targets:
            try:
                await connection.send_json({"type": "system", "content": reason})
                await connection.close(code=1000)
            except (RuntimeError, ConnectionError):
                # 이미 닫힌 커넥션은 건너뛴다.
                continue
