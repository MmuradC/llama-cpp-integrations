/**
 * Deleting messages from inside a conversation — the planning half of the
 * panel's `/delete` and `/prune` commands.
 *
 * Everything here is pure: it takes message arrays and returns *what would
 * happen*. Two reasons it is split out from the commands themselves:
 *
 *   - Destructive operations get a dry run first. The command bodies print a
 *     plan and only apply one when the user repeats the command with
 *     `confirm`, so "what does this remove" has to be answerable without
 *     touching the database.
 *   - It is testable without IndexedDB (tests/unit/message-deletion.test.ts).
 *
 * Why these commands exist at all: llama.cpp's own `/compact` (this project's
 * compaction module) never deletes anything. It writes a summary message and
 * points `conversation.compactedThroughMessageId` at it, which trims what is
 * *sent* — the original messages stay in IndexedDB and stay on screen forever.
 * That is the right default, but it means the stored conversation (and every
 * later re-summarisation of it) keeps growing with no way to shed it from the
 * UI: the per-message trash icon upstream deletes a message *and everything
 * after it*, which is the opposite of what "remove this one bulky turn"
 * wants. `/delete` and `/prune` are the missing other half.
 *
 * The three operations, in the order of how much they destroy:
 *
 *   - **in place** (`/delete <n>`): remove exactly one message and re-parent
 *     its children to its parent. Everything else survives, including other
 *     branches hanging off it. This is the "drop that one huge tool dump"
 *     case.
 *   - **tail** (`/delete last <n>`): remove the newest n messages of the
 *     active branch, i.e. upstream's own cascading delete, anchored n from
 *     the end.
 *   - **prune** (`/prune`): remove everything older than the compaction
 *     summary and re-parent the summary to the conversation root. The
 *     summary and everything after it stay, so what gets sent does not change
 *     at all — what changes is that the conversation is now short enough to
 *     re-compact cheaply instead of re-summarising its whole history again.
 *
 * Tree walkers are local, deliberately. `$lib/utils` exports
 * findDescendantMessages / findLeafNode / findMessageById, but this module has
 * to compile against several llama.cpp versions (and be testable in a plain
 * node environment), and those helpers' export surface moves between them.
 * The three walks needed here are a dozen lines each.
 */

/** The fields these planners read. `DatabaseMessage` satisfies it structurally. */
export interface DeletableMessage {
	id: string;
	parent: string | null;
	children: string[];
	content: string;
	role: string;
	timestamp: number;
	type?: string;
}

/** Role tally, for "3 user messages, 2 assistant replies" style reporting. */
export interface RoleCounts {
	total: number;
	user: number;
	assistant: number;
	system: number;
	other: number;
}

export interface DeletionPlan {
	/** Every message id that will be removed. */
	deleteIds: string[];
	/**
	 * Set when the deletion is not a cascade: `id` must be re-parented to
	 * `parentId` and, for the parent, adopted in place of the deleted child
	 * (see `adoptInto`).
	 */
	reparent?: { id: string; parentId: string };
	/** Children the surviving parent adopts, in the deleted message's slot. */
	adoptInto?: { parentId: string; children: string[] };
	/** For prune: the surviving message that becomes the root's only child. */
	rootChildrenAfter?: string[];
	/** The message the user aimed at, for the dry-run preview. */
	target: DeletableMessage;
	/** Where the conversation's currNode has to move, when it was removed. */
	nextCurrentNode?: string;
	counts: RoleCounts;
	/** Characters in everything being deleted — the "space freed" figure. */
	chars: number;
	/** True when this deletion drops the message requests are trimmed to. */
	dropsCompactionSummary: boolean;
}

export type PlanResult = { ok: true; plan: DeletionPlan } | { ok: false; reason: string };

/** Ids of every descendant of `id` (children, grandchildren, …), never `id`. */
export function collectDescendants(all: DeletableMessage[], id: string): string[] {
	const found: string[] = [];
	const seen = new Set<string>([id]);
	const queue = [id];

	while (queue.length > 0) {
		const current = queue.pop()!;
		const message = all.find((m) => m.id === current);

		if (!message) continue;

		for (const childId of message.children ?? []) {
			if (seen.has(childId)) continue;

			seen.add(childId);
			found.push(childId);
			queue.push(childId);
		}
	}

	return found;
}

/** Follows first children down from `id` to the deepest reachable message. */
export function findLeafNode(all: DeletableMessage[], id: string): string {
	let current = id;
	const seen = new Set<string>([id]);

	for (;;) {
		const message = all.find((m) => m.id === current);
		const next = message?.children?.[0];

		if (!next || seen.has(next)) return current;

		seen.add(next);
		current = next;
	}
}

/** The conversation's genesis message: `type === 'root'`, no parent. */
export function rootMessageOf(all: DeletableMessage[]): DeletableMessage | undefined {
	return all.find((m) => m.type === 'root' && m.parent === null);
}

export function countRoles(messages: DeletableMessage[]): RoleCounts {
	const counts: RoleCounts = { assistant: 0, other: 0, system: 0, total: messages.length, user: 0 };

	for (const message of messages) {
		if (message.role === 'user') counts.user++;
		else if (message.role === 'assistant') counts.assistant++;
		else if (message.role === 'system') counts.system++;
		else counts.other++;
	}

	return counts;
}

/** Clips content for the one-line dry-run preview. */
export function previewOf(message: DeletableMessage, limit = 160): string {
	const flat = message.content.replace(/\s+/g, ' ').trim();

	if (!flat) return '(no text content)';

	return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}

/** "3 messages (2 user, 1 assistant), ~4.1k characters" */
export function describeSize(counts: RoleCounts, chars: number): string {
	const parts: string[] = [];

	if (counts.user > 0) parts.push(`${counts.user} user`);
	if (counts.assistant > 0) parts.push(`${counts.assistant} assistant`);
	if (counts.system > 0) parts.push(`${counts.system} system`);
	if (counts.other > 0) parts.push(`${counts.other} other`);

	const breakdown = parts.length > 1 ? ` (${parts.join(', ')})` : '';

	return `${counts.total} message${counts.total === 1 ? '' : 's'}${breakdown}, ${formatChars(chars)}`;
}

export function formatChars(chars: number): string {
	if (chars < 1000) return `${chars} characters`;

	return `~${(chars / 1000).toFixed(1)}k characters`;
}

function buildPlan(
	all: DeletableMessage[],
	target: DeletableMessage,
	deleteIds: string[],
	options: {
		compactionPoint?: string;
		adoptInto?: { parentId: string; children: string[] };
		nextCurrentNode?: string;
		reparent?: { id: string; parentId: string };
		rootChildrenAfter?: string[];
	}
): DeletionPlan {
	const unique = [...new Set(deleteIds)];
	const removed = all.filter((m) => unique.includes(m.id));

	return {
		adoptInto: options.adoptInto,
		chars: removed.reduce((total, m) => total + m.content.length, 0),
		counts: countRoles(removed),
		deleteIds: unique,
		dropsCompactionSummary: !!options.compactionPoint && unique.includes(options.compactionPoint),
		nextCurrentNode: options.nextCurrentNode,
		reparent: options.reparent,
		rootChildrenAfter: options.rootChildrenAfter,
		target
	};
}

/**
 * Plan for `/delete <n>`: remove the n-th message of the active branch
 * (1-based, oldest first) and keep everything else.
 *
 * The message's children are re-parented to its own parent and adopted in its
 * slot, so the branch that continues through it stays reachable. Refuses to
 * touch the conversation root, and refuses a position that is not on the
 * active branch (deleting a message the user cannot see would be a surprise).
 */
export function planInPlaceDelete(
	all: DeletableMessage[],
	activePath: DeletableMessage[],
	position: number,
	options: { compactionPoint?: string; currentNodeId?: string | null } = {}
): PlanResult {
	if (activePath.length === 0) return { ok: false, reason: 'This conversation has no messages yet' };

	if (!Number.isInteger(position) || position < 1)
		return { ok: false, reason: `"${position}" is not a message number — use /delete <n> with n ≥ 1` };

	if (position > activePath.length)
		return {
			ok: false,
			reason: `This branch has ${activePath.length} message${
				activePath.length === 1 ? '' : 's'
			} — /delete ${activePath.length} is the newest one`
		};

	const target = activePath[position - 1];

	if (target.type === 'root')
		return { ok: false, reason: 'That is the conversation root — it cannot be deleted' };

	const root = rootMessageOf(all);
	const parentId = target.parent ?? root?.id;

	if (!parentId)
		return { ok: false, reason: 'That message has no parent to re-attach its replies to' };

	const children = target.children ?? [];
	// Only children that still exist count as adopted: a dangling id in the
	// array (a message deleted earlier by another path) must not be re-attached.
	const liveChildren = children.filter((id) => all.some((m) => m.id === id));
	const nextCurrentNode =
		options.currentNodeId && options.currentNodeId === target.id
			? liveChildren.length > 0
				? findLeafNode(all, liveChildren[0])
				: parentId
			: undefined;

	return {
		ok: true,
		plan: buildPlan(all, target, [target.id], {
			adoptInto: { children: liveChildren, parentId },
			compactionPoint: options.compactionPoint,
			nextCurrentNode,
			reparent: undefined
		})
	};
}

/**
 * Plan for `/delete last <n>`: the newest n messages of the active branch.
 *
 * Applied through the app's own `chatStore.deleteMessage(anchorId)`, which
 * cascades to the anchor's descendants and re-points currNode at a surviving
 * sibling — so this only has to name the anchor and count what that cascade
 * will take, which is why the count comes from `collectDescendants` and not
 * from n (branch variants hanging off the anchor go too).
 */
export function planTailDelete(
	all: DeletableMessage[],
	activePath: DeletableMessage[],
	count = 1,
	options: { compactionPoint?: string; currentNodeId?: string | null } = {}
): PlanResult {
	if (activePath.length === 0) return { ok: false, reason: 'This conversation has no messages yet' };

	if (!Number.isInteger(count) || count < 1)
		return { ok: false, reason: `"${count}" is not a count — use /delete last <n> with n ≥ 1` };

	if (count > activePath.length)
		return {
			ok: false,
			reason: `This branch has ${activePath.length} message${
				activePath.length === 1 ? '' : 's'
			}, so /delete last ${count} would empty it — use /clear for a new chat`
		};

	const anchor = activePath[activePath.length - count];
	const deleteIds = [anchor.id, ...collectDescendants(all, anchor.id)];

	return {
		ok: true,
		plan: buildPlan(all, anchor, deleteIds, { compactionPoint: options.compactionPoint })
	};
}

/**
 * Importance heuristics for prune planning — whether an old USER/ASSISTANT
 * message looks like a fact worth keeping as a message, not just as part of
 * the summary. Same philosophy as the tool distiller: deterministic, testable,
 * regex-grade. Paths, commands, code blocks, and substantial prose survive;
 * greetings, one-word prompts, and bare tool receipts do not.
 */
export const IMPORTANT_USER_MIN_CHARS = 200;
export const IMPORTANT_ASSISTANT_MIN_CHARS = 800;

const CODE_FENCE_RE = /```/;
const PATHISH_RE = /(^\s*\/?[\w.~-]+\/|\/(home|tmp|etc|opt|var)\/|https?:\/\/|\bdecid(ed|e|es)\b)/i;
const DECISION_RE = /\b(decid(ed|e|es)|plan|agreed|approved|chosen|option [ab]|approach|will use|should use|root cause|fix:|the fix|bug:|issue:)\b/i;

export function looksImportant(m: DeletableMessage): boolean {
	const text = typeof m.content === 'string' ? m.content : '';
	if (m.role === 'user') {
		return (
			text.length >= IMPORTANT_USER_MIN_CHARS ||
			PATHISH_RE.test(text) ||
			DECISION_RE.test(text)
		);
	}
	if (m.role === 'assistant') {
		return (
			text.length >= IMPORTANT_ASSISTANT_MIN_CHARS ||
			CODE_FENCE_RE.test(text) ||
			DECISION_RE.test(text) ||
			// any toolCalls means this assistant turn shaped state somewhere —
			// deleting it would orphan the reasoning around its tool calls
			Boolean(m.toolCalls)
		);
	}
	return false;
}

/**
 * Plan for `/prune`: drop everything older than `compactionPointId` (the
 * summary `/compact` wrote) and re-parent the summary to the conversation
 * root.
 *
 * The kept set is the summary *and its descendants*, not "the active branch
 * from the summary onwards": the summary is usually on the active branch, but
 * navigating to an older sibling branch leaves it off-path, and pruning the
 * branch the user happens to be looking at instead of the summary's own
 * subtree would delete the summary. Everything reachable from the summary
 * survives, including side branches below it.
 *
 * `keepImportant`: when set, messages judged important (see looksImportant)
 * are ALSO kept as first-class messages instead of being folded into the
 * summary. `/compact`'s auto-prune sets this so the summary replaces context,
 * but the user's own task statements, decisions, and long assistant answers
 * stay visible in the transcript. `/prune` without the flag stays the strict
 * cleanup it always was.
 */
export function planPruneToSummary(
	all: DeletableMessage[],
	compactionPointId: string | undefined,
	options: { currentNodeId?: string | null; keepImportant?: boolean } = {}
): PlanResult {
	if (!compactionPointId)
		return {
			ok: false,
			reason: 'This conversation has not been compacted — run /compact first, then /prune'
		};

	const summary = all.find((m) => m.id === compactionPointId);

	if (!summary)
		return {
			ok: false,
			reason:
				'The compaction summary this conversation points at no longer exists, so there is nothing safe to prune — run /compact again'
		};

	const root = rootMessageOf(all);

	if (!root) return { ok: false, reason: 'This conversation has no root message to re-attach to' };

	if (summary.id === root.id)
		return { ok: false, reason: 'The compaction summary is the conversation root — nothing to prune' };

	const keep = new Set([
		summary.id,
		...collectDescendants(all, summary.id),
		...(options.keepImportant ? all.filter(looksImportant).map((m) => m.id) : [])
	]);
	const deleteIds = all.filter((m) => m.id !== root.id && !keep.has(m.id)).map((m) => m.id);

	if (deleteIds.length === 0)
		return {
			ok: false,
			reason: options.keepImportant
				? 'Every older message is judged important \u2014 nothing safe to remove. /prune (without important-keeping) removes all of them instead.'
				: 'There is nothing older than the compaction summary to remove'
		};

	const currentNodeId = options.currentNodeId;
	const nextCurrentNode =
		currentNodeId && deleteIds.includes(currentNodeId) ? findLeafNode(all, summary.id) : undefined;

	return {
		ok: true,
		plan: buildPlan(all, summary, deleteIds, {
			nextCurrentNode,
			reparent: { id: summary.id, parentId: root.id },
			rootChildrenAfter: [summary.id]
		})
	};
}

/**
 * Parse `/delete` arguments. Accepted forms (case-insensitive, extra spaces
 * ignored):
 *
 *   ``            → no plan; the command prints what the two forms do
 *   `5`           → in place, the 5th message of this branch
 *   `last`        → tail, 1 message
 *   `last 3`      → tail, 3 messages
 *   `… confirm`   → same, but applied instead of described
 */
export type DeleteArgs =
	| { kind: 'error'; error: string }
	| { kind: 'in-place'; position: number; confirmed: boolean }
	| { kind: 'tail'; count: number; confirmed: boolean }
	| { kind: 'usage' };

export function parseDeleteArgs(raw: string): DeleteArgs {
	const tokens = raw.trim().toLowerCase().split(/\s+/).filter(Boolean);
	const confirmed = tokens.includes('confirm');
	const rest = tokens.filter((token) => token !== 'confirm');

	if (rest.length === 0) return { kind: 'usage' };

	if (rest[0] === 'last') {
		if (rest.length === 1) return { count: 1, confirmed, kind: 'tail' };

		if (rest.length > 2)
			return { error: `Could not read "${raw.trim()}" — try /delete last 3`, kind: 'error' };

		const count = Number(rest[1]);

		if (!Number.isInteger(count) || count < 1)
			return { error: `"${rest[1]}" is not a count — try /delete last 3`, kind: 'error' };

		return { count, confirmed, kind: 'tail' };
	}

	if (rest.length > 1)
		return {
			error: `Could not read "${raw.trim()}" — try /delete 12 or /delete last 3`,
			kind: 'error'
		};

	const position = Number(rest[0]);

	if (!Number.isInteger(position) || position < 1)
		return {
			error: `"${rest[0]}" is not a message number — try /delete 12 or /delete last 3`,
			kind: 'error'
		};

	return { confirmed, kind: 'in-place', position };
}
