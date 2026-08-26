"""TicketActionService: deterministic domain actions (V2 collaboration).

Every action is a unit of work:
    Business Effect (Ticket + TicketEvent + Outbox) in ONE transaction
        -> commit
        -> notification dispatch happens after commit (caller)

Actions: CLAIM / RESOLVE / REQUESTER_CONFIRM / REJECT_RESOLUTION /
ESCALATE / FORCE_CLOSE / APPROVE / REJECT. Agent advice never executes
these directly (invariant #4).
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from app.application.approval_service import ApprovalService
from app.application.memory_service import MemoryService
from app.application.notification_service import NotificationService
from app.application.target_resolver import ResolvedTarget, TargetResolver
from app.application.ticket_service import new_ticket_id
from app.domain.conversation import Conversation
from app.domain.notification import NotificationType, Visibility
from app.domain.pending_action import ApprovableAction, PendingAction, PendingActionStatus
from app.domain.ticket import AlreadyClaimed, InvalidStateTransition, Ticket, TicketEventType, TicketStatus
from app.infrastructure.repositories import (
    ApprovalRepository,
    PendingActionRepository,
    TicketStore,
    UserRepository,
    txn,
)


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex[:12]}"


@dataclass
class ActionOutcome:
    ticket: Ticket | None = None
    reply: str = ""
    approval_id: str | None = None
    event_type: str | None = None


class TicketActionService:
    """Executes domain actions and coordinates notifications.

    Role checks are performed by the calling layer (channel/workflow);
    this service is the deterministic executor.
    """

    def __init__(
        self,
        conn,
        store: TicketStore,
        users: UserRepository,
        approvals: ApprovalService,
        approval_repo: ApprovalRepository,
        pending_actions: PendingActionRepository,
        memory: MemoryService,
        notifications: NotificationService,
        targets: TargetResolver,
    ) -> None:
        self._conn = conn
        self._store = store
        self._users = users
        self._approvals = approvals
        self._approval_repo = approval_repo
        self._pending = pending_actions
        self._memory = memory
        self._notifications = notifications
        self._targets = targets

    # --- requester-side ticket creation (workflow calls this) ---

    def create_ticket(
        self,
        requester_user_id: str,
        text: str,
        conversation: Conversation,
        *,
        requester_name: str,
        trace_id: str,
        source: str = "message",
    ) -> Ticket:
        ticket = Ticket(
            id=new_ticket_id(self._store),
            user_id=requester_user_id,
            title=text,
            description=text,
            source_conversation_id=conversation.channel_conversation_id,
            queue=conversation.queue,
        )
        with txn(self._conn):
            stored = self._store.create(ticket, trace_id=trace_id, conversation_id=conversation.channel_conversation_id)
            event_id = f"{stored.id}:created:{trace_id}"
            operator = self._targets.operator_queue(stored.queue, channel=conversation.channel)
            # Phase A enqueues only the deterministic operator work item.
            # The requester-facing receipt/detail (agent-drafted, honest)
            # is enqueued in phase B via requester_acknowledgement().
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.OPERATOR_WORK_ITEM,
                visibility=Visibility.INTERNAL,
                message=(
                    f"新工单 {stored.id}\n\n"
                    f"优先级：P3\n"
                    f"队列：{stored.queue or 'general'}\n"
                    f"报修人：{requester_name}\n\n"
                    f"摘要：\n{stored.title}\n\n可执行：CLAIM {stored.id}"
                ),
                target=operator,
                ticket_id=stored.id,
                trace_id=trace_id,
            )
        return stored

    def requester_acknowledgement(
        self,
        ticket: Ticket,
        requester_user_id: str,
        *,
        reply: str,
        private_detail: str,
        source_event_id: str,
        trace_id: str,
        channel: str,
    ) -> None:
        """Phase-B requester-facing notifications (V2.1 honest receipt).

        The PUBLIC receipt is the agent-drafted reply (it never claims
        "私发给你" — the agent prompt forbids claiming unproven states).
        PRIVATE_DETAIL is enqueued ONLY when a private target actually
        resolves (H7: no more silent drop + lying receipt).
        """
        with txn(self._conn):
            public = self._targets.requester_public(ticket, channel=channel)
            private = self._targets.requester_private(ticket, requester_user_id, channel=channel)
            self._notifications.enqueue(
                source_event_id=source_event_id,
                notification_type=NotificationType.REACTIVE_REPLY,
                visibility=Visibility.PUBLIC,
                message=reply,
                target=public,
                ticket_id=ticket.id,
                trace_id=trace_id,
            )
            if private.delivery is not None:
                self._notifications.enqueue(
                    source_event_id=source_event_id,
                    notification_type=NotificationType.PRIVATE_DETAIL,
                    visibility=Visibility.PRIVATE,
                    message=private_detail,
                    target=private,
                    ticket_id=ticket.id,
                    trace_id=trace_id,
                )

    def conversation_reply(
        self,
        *,
        channel: str,
        conversation_id: str,
        text: str,
        trace_id: str,
        source_event_id: str,
    ) -> None:
        """Phase-B reply delivery for the no-ticket FAQ path."""
        with txn(self._conn):
            target = self._targets.by_conversation(channel, conversation_id)
            self._notifications.enqueue(
                source_event_id=source_event_id,
                notification_type=NotificationType.REACTIVE_REPLY,
                visibility=Visibility.PUBLIC,
                message=text,
                target=target,
                trace_id=trace_id,
            )

    def operator_update_note(
        self,
        ticket: Ticket,
        *,
        note: str,
        source_event_id: str,
        trace_id: str,
        channel: str,
    ) -> None:
        """Phase-B operator-side update (continuation/priority changes)."""
        with txn(self._conn):
            operator = self._targets.operator_queue(ticket.queue, channel=channel)
            self._notifications.enqueue(
                source_event_id=source_event_id,
                notification_type=NotificationType.INTERNAL_NOTE,
                visibility=Visibility.INTERNAL,
                message=note,
                target=operator,
                ticket_id=ticket.id,
                trace_id=trace_id,
            )

    # --- operator actions ---

    def claim(
        self,
        ticket_id: str,
        actor_user_id: str,
        *,
        trace_id: str,
        channel: str = "wecom",
        conversation_id: str | None = None,
    ) -> ActionOutcome:
        actor_name = self._user_name(actor_user_id)
        with txn(self._conn):
            ticket = self._store.claim(
                ticket_id,
                actor_user_id,
                actor_user_id=actor_user_id,
                trace_id=trace_id,
                conversation_id=conversation_id,
            )
            event_id = f"{ticket_id}:claimed:{trace_id}"
            origin = self._origin(channel, conversation_id)
            public = self._targets.requester_public(ticket, channel=channel)
            private = self._targets.requester_private(ticket, ticket.user_id, channel=channel)
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.OPERATOR_ACTION_RECEIPT,
                visibility=Visibility.INTERNAL,
                message=f"认领成功：\n{ticket_id}\n当前处理人员：{actor_name}",
                target=origin,
                ticket_id=ticket_id,
                trace_id=trace_id,
            )
            requester_name = self._user_name(ticket.user_id)
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.REQUESTER_STATUS_UPDATE,
                visibility=Visibility.PUBLIC,
                message=f"{requester_name}，工单 {ticket_id} 已由 {actor_name} 接手处理。",
                target=public,
                ticket_id=ticket_id,
                trace_id=trace_id,
            )
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.REQUESTER_STATUS_UPDATE,
                visibility=Visibility.PRIVATE,
                message=f"工单 {ticket_id} 已由 {actor_name} 接手处理。",
                target=private,
                ticket_id=ticket_id,
                trace_id=trace_id,
            )
        return ActionOutcome(ticket=ticket, reply=f"认领成功：{ticket_id}，当前处理人员：{actor_name}。", event_type="claimed")

    def resolve(
        self,
        ticket_id: str,
        actor_user_id: str,
        note: str | None,
        *,
        trace_id: str,
        channel: str = "wecom",
        conversation_id: str | None = None,
    ) -> ActionOutcome:
        with txn(self._conn):
            ticket = self._store.transition(
                ticket_id,
                TicketStatus.RESOLVED,
                {"note": note} if note else None,
                actor_user_id=actor_user_id,
                trace_id=trace_id,
                conversation_id=conversation_id,
            )
            event_id = f"{ticket_id}:resolved:{trace_id}"
            if note:
                base = ticket.summary or ticket.title or ticket.description
                self._store.set_operational(
                    ticket_id,
                    summary=f"{base}\n处理结果：{note}"[:600],
                )
            origin = self._origin(channel, conversation_id)
            public = self._targets.requester_public(ticket, channel=channel)
            private = self._targets.requester_private(ticket, ticket.user_id, channel=channel)
            requester_name = self._user_name(ticket.user_id)
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.OPERATOR_ACTION_RECEIPT,
                visibility=Visibility.INTERNAL,
                message=f"处理完成：\n{ticket_id}\n{note or ''}",
                target=origin,
                ticket_id=ticket_id,
                trace_id=trace_id,
            )
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.REQUESTER_CONFIRMATION_REQUEST,
                visibility=Visibility.PUBLIC,
                message=f"{requester_name}，工单 {ticket_id} 已处理完成，请确认是否恢复正常。",
                target=public,
                ticket_id=ticket_id,
                trace_id=trace_id,
            )
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.REQUESTER_CONFIRMATION_REQUEST,
                visibility=Visibility.PRIVATE,
                message=f"工单：{ticket_id}\n问题：{ticket.title}\n状态：RESOLVED\n请回复确认，或说明还未恢复。",
                target=private,
                ticket_id=ticket_id,
                trace_id=trace_id,
            )
        return ActionOutcome(ticket=ticket, reply=f"工单 {ticket_id} 已标记处理完成，等待用户确认。", event_type="resolved")

    # --- requester actions ---

    def requester_confirm(
        self,
        ticket_id: str,
        requester_user_id: str,
        *,
        trace_id: str,
        channel: str = "wecom",
        conversation_id: str | None = None,
    ) -> ActionOutcome:
        with txn(self._conn):
            ticket = self._store.transition(
                ticket_id,
                TicketStatus.CLOSED,
                {"confirmed_by": requester_user_id},
                actor_user_id=requester_user_id,
                trace_id=trace_id,
                conversation_id=conversation_id,
            )
            memories = self._memory.remember(ticket_id, source="confirmed_closure")
            self._trace_memory_extract(trace_id, ticket_id, memories)
            event_id = f"{ticket_id}:closed:{trace_id}"
            public = self._targets.requester_public(ticket, channel=channel)
            private = self._targets.requester_private(ticket, requester_user_id, channel=channel)
            operator = self._targets.operator_queue(ticket.queue, channel=channel)
            requester_name = self._user_name(requester_user_id)
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.REQUESTER_STATUS_UPDATE,
                visibility=Visibility.PUBLIC,
                message=f"{requester_name}，工单 {ticket_id} 已确认关闭。",
                target=public,
                ticket_id=ticket_id,
                trace_id=trace_id,
            )
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.REQUESTER_STATUS_UPDATE,
                visibility=Visibility.PRIVATE,
                message=f"工单 {ticket_id} 已关闭，感谢您的反馈。",
                target=private,
                ticket_id=ticket_id,
                trace_id=trace_id,
            )
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.INTERNAL_NOTE,
                visibility=Visibility.INTERNAL,
                message=f"工单 {ticket_id} 已由用户确认关闭。",
                target=operator,
                ticket_id=ticket_id,
                trace_id=trace_id,
            )
        return ActionOutcome(ticket=ticket, reply=f"工单 {ticket_id} 已确认关闭。", event_type="closed")

    def reject_resolution(
        self,
        ticket_id: str,
        requester_user_id: str,
        reason: str | None,
        *,
        trace_id: str,
        channel: str = "wecom",
        conversation_id: str | None = None,
    ) -> ActionOutcome:
        with txn(self._conn):
            ticket = self._store.transition(
                ticket_id,
                TicketStatus.IN_PROGRESS,
                {"reason": reason} if reason else None,
                actor_user_id=requester_user_id,
                trace_id=trace_id,
                conversation_id=conversation_id,
            )
            event_id = f"{ticket_id}:rejected:{trace_id}"
            public = self._targets.requester_public(ticket)
            operator = self._targets.operator_queue(ticket.queue, channel=channel)
            requester_name = self._user_name(requester_user_id)
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.REQUESTER_STATUS_UPDATE,
                visibility=Visibility.PUBLIC,
                message=f"{requester_name}，已收到，工单 {ticket_id} 会继续处理。",
                target=public,
                ticket_id=ticket_id,
                trace_id=trace_id,
            )
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.INTERNAL_NOTE,
                visibility=Visibility.INTERNAL,
                message=f"工单 {ticket_id} 用户反馈未恢复：{reason or '未说明原因'}，已重新进入处理。",
                target=operator,
                ticket_id=ticket_id,
                trace_id=trace_id,
            )
        return ActionOutcome(ticket=ticket, reply=f"已收到，工单 {ticket_id} 会继续处理。", event_type="resolution_rejected")

    # --- HITL: escalate / force_close -> approval -> execution ---

    def escalate(
        self,
        ticket_id: str,
        actor_user_id: str,
        reason: str | None,
        *,
        trace_id: str,
        channel: str = "wecom",
        conversation_id: str | None = None,
    ) -> ActionOutcome:
        return self._request_approval(
            ApprovableAction.ESCALATE, ticket_id, actor_user_id, reason, trace_id, channel, conversation_id
        )

    def force_close(
        self,
        ticket_id: str,
        actor_user_id: str,
        reason: str | None,
        *,
        trace_id: str,
        channel: str = "wecom",
        conversation_id: str | None = None,
    ) -> ActionOutcome:
        if not reason:
            raise InvalidStateTransition("force_close requires a reason")
        ticket = self._store.get(ticket_id)
        if ticket is None:
            raise KeyError(f"ticket not found: {ticket_id}")
        # Policy: the state machine only allows IN_PROGRESS/RESOLVED -> CLOSED.
        # An OPEN ticket can never be force-closed by any path (V2.1 closure fix).
        if ticket.status.value not in ("IN_PROGRESS", "RESOLVED"):
            raise InvalidStateTransition(
                f"force_close not allowed from {ticket.status.value}"
            )
        return self._request_approval(
            ApprovableAction.FORCE_CLOSE, ticket_id, actor_user_id, reason, trace_id, channel, conversation_id
        )

    def _request_approval(
        self,
        action_type: ApprovableAction,
        ticket_id: str,
        actor_user_id: str,
        reason: str | None,
        trace_id: str,
        channel: str,
        conversation_id: str | None,
    ) -> ActionOutcome:
        with txn(self._conn):
            if self._store.get(ticket_id) is None:
                raise KeyError(f"ticket not found: {ticket_id}")
            approval = self._approvals.escalate(
                ticket_id,
                action=action_type.value,
                requested_by=actor_user_id,
                reason=reason,
            )
            self._pending.create(
                PendingAction(
                    id=new_id("pa_"),
                    ticket_id=ticket_id,
                    action_type=action_type,
                    payload={"reason": reason} if reason else None,
                    requested_by=actor_user_id,
                    approval_id=approval.id,
                )
            )
            event_id = f"{ticket_id}:{action_type.value.lower()}:{approval.id}"
            origin = self._origin(channel, conversation_id)
            approver = self._targets.approver()
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.APPROVAL_REQUEST,
                visibility=Visibility.INTERNAL,
                message=(
                    f"审批请求：\n工单：{ticket_id}\n动作：{action_type.value}\n"
                    f"原因：{reason or '未说明'}\n\n"
                    f"可执行：/approve {approval.id}  或  /reject {approval.id}"
                ),
                target=approver,
                ticket_id=ticket_id,
                trace_id=trace_id,
            )
            self._notifications.enqueue(
                source_event_id=event_id,
                notification_type=NotificationType.OPERATOR_ACTION_RECEIPT,
                visibility=Visibility.INTERNAL,
                message=f"已发起审批：{approval.id}（{action_type.value}），工单状态未改变。",
                target=origin,
                ticket_id=ticket_id,
                trace_id=trace_id,
            )
        return ActionOutcome(
            ticket=self._store.get(ticket_id),
            reply=f"已发起审批：{approval.id}。",
            approval_id=approval.id,
            event_type="approval_requested",
        )

    def approve(
        self,
        approval_id: str,
        decided_by: str,
        *,
        trace_id: str,
        channel: str = "wecom",
        conversation_id: str | None = None,
    ) -> ActionOutcome:
        with txn(self._conn):
            approval = self._approvals.approve(approval_id, decided_by=decided_by)
            pending = self._pending_awaiting(approval_id)
            if pending is None:
                return ActionOutcome(reply=f"审批 {approval_id} 已通过。", event_type="approval_approved")
            if not self._pending.mark_executed(pending.id):
                return ActionOutcome(reply=f"审批 {approval_id} 已通过。", event_type="approval_approved")
            outcome = self._execute(pending, approval, channel, conversation_id, trace_id)
        outcome.reply = f"审批 {approval_id} 已通过并执行：{outcome.event_type}。"
        return outcome

    def reject(
        self,
        approval_id: str,
        decided_by: str,
        reason: str | None,
        *,
        trace_id: str,
        channel: str = "wecom",
        conversation_id: str | None = None,
    ) -> ActionOutcome:
        with txn(self._conn):
            approval = self._approvals.reject(approval_id, decided_by=decided_by, reason=reason)
            pending = self._pending_awaiting(approval_id)
            if pending is not None:
                self._pending.mark_skipped(pending.id)
            ticket = self._store.get(approval.ticket_id)
            origin = self._origin(channel, conversation_id)
            self._notifications.enqueue(
                source_event_id=f"{approval_id}:rejected:{trace_id}",
                notification_type=NotificationType.APPROVAL_RESULT,
                visibility=Visibility.INTERNAL,
                message=f"审批已驳回：{approval_id}\n原因：{reason or '未说明'}",
                target=origin,
                ticket_id=approval.ticket_id,
                trace_id=trace_id,
            )
        return ActionOutcome(ticket=ticket, reply=f"审批 {approval_id} 已驳回。", event_type="approval_rejected")

    def _execute(
        self,
        pending: PendingAction,
        approval,
        channel: str,
        conversation_id: str | None,
        trace_id: str,
    ) -> ActionOutcome:
        if pending.action_type == ApprovableAction.ESCALATE:
            ticket = self._store.get(pending.ticket_id)
            self._store.add_event(
                pending.ticket_id,
                TicketEventType.ESCALATED,
                {
                    "approval_id": approval.id,
                    "reason": (pending.payload or {}).get("reason"),
                    "decided_by": approval.decided_by,
                },
                actor_user_id=approval.decided_by,
                trace_id=trace_id,
                conversation_id=conversation_id,
            )
            origin = self._origin(channel, conversation_id)
            operator = self._targets.operator_queue(ticket.queue if ticket else None)
            self._notifications.enqueue(
                source_event_id=f"{approval.id}:executed:{trace_id}",
                notification_type=NotificationType.APPROVAL_RESULT,
                visibility=Visibility.INTERNAL,
                message=f"工单 {pending.ticket_id} 已升级（审批 {approval.id} 通过，审批人：{approval.decided_by}）。",
                target=operator,
                ticket_id=pending.ticket_id,
                trace_id=trace_id,
            )
            self._notifications.enqueue(
                source_event_id=f"{approval.id}:executed:{trace_id}",
                notification_type=NotificationType.OPERATOR_ACTION_RECEIPT,
                visibility=Visibility.INTERNAL,
                message=f"工单 {pending.ticket_id} 已升级。",
                target=origin,
                ticket_id=pending.ticket_id,
                trace_id=trace_id,
            )
            return ActionOutcome(ticket=ticket, event_type="escalated")

        if pending.action_type == ApprovableAction.FORCE_CLOSE:
            reason = (pending.payload or {}).get("reason") or "force close"
            ticket = self._store.transition(
                pending.ticket_id,
                TicketStatus.CLOSED,
                {"reason": reason, "approval_id": approval.id, "decided_by": approval.decided_by},
                actor_user_id=approval.decided_by,
                trace_id=trace_id,
                conversation_id=conversation_id,
            )
            self._trace_memory_extract(
                trace_id, pending.ticket_id, self._memory.remember(pending.ticket_id, source="force_closed")
            )
            operator = self._targets.operator_queue(ticket.queue, channel=channel)
            public = self._targets.requester_public(ticket)
            requester_name = self._user_name(ticket.user_id)
            self._notifications.enqueue(
                source_event_id=f"{approval.id}:executed:{trace_id}",
                notification_type=NotificationType.INTERNAL_NOTE,
                visibility=Visibility.INTERNAL,
                message=f"工单 {pending.ticket_id} 已强制关闭（原因：{reason}）。",
                target=operator,
                ticket_id=pending.ticket_id,
                trace_id=trace_id,
            )
            self._notifications.enqueue(
                source_event_id=f"{approval.id}:executed:{trace_id}",
                notification_type=NotificationType.REQUESTER_STATUS_UPDATE,
                visibility=Visibility.PUBLIC,
                message=f"{requester_name}，工单 {pending.ticket_id} 已关闭（原因：{reason}）。",
                target=public,
                ticket_id=pending.ticket_id,
                trace_id=trace_id,
            )
            return ActionOutcome(ticket=ticket, event_type="force_closed")

        raise RuntimeError(f"no executor for action {pending.action_type}")

    def _pending_awaiting(self, approval_id: str) -> PendingAction | None:
        for action in self._pending.list_all():
            if action.approval_id == approval_id:
                return action
        return None

    # --- helpers ---

    def _origin(self, channel: str, conversation_id: str | None) -> ResolvedTarget:
        if conversation_id:
            return self._targets.action_origin(channel, conversation_id)
        return ResolvedTarget(None, "no_action_origin")

    def _user_name(self, user_id: str) -> str:
        user = self._users.get(user_id)
        return user.display_name if user else user_id

    def _trace_memory_extract(self, trace_id: str, ticket_id: str, memories) -> None:
        from app.infrastructure.trace import TraceLogger

        try:
            TraceLogger(self._conn).event(
                trace_id, "memory_extract", {"ticket_id": ticket_id, "facts": [m.fact for m in memories]}
            )
        except Exception:
            pass
