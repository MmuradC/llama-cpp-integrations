/**
 * chatPermissionStore - per-chat permission modes, panel-owned.
 *
 * Wires the panel's own storage (see ./db.ts) into the app's
 * permissionModeStore, which the agentic gate consults with the id of the
 * conversation a tool call belongs to. The app therefore decides per chat,
 * while the storage stays out of llama.cpp's tree.
 *
 * One thing here is deliberately global, and it is the reason this file also
 * mirrors: the `files` MCP server gates its own writes by reading the panel's
 * *global* mode file, and a tool call reaching that server carries no
 * conversation id at all - it only sees a path and some chunks. So the panel
 * keeps that file in step with the chat the user is actually looking at. The
 * in-app gate is per-chat regardless; only the files-server gate follows the
 * active chat (and with several chats running at once, the most recent one).
 */

import { conversationsStore } from '$lib/stores';
import { incognitoChatStore } from '$lib/stores/incognito-chat.svelte';
import { permissionModeStore } from '$lib/stores/permission-mode.svelte';
import { chatPermissionDb, type ChatPermissionMode } from './db';

/** Same origin the rest of the panel uses (see RightBar.svelte, upload-image). */
const PANEL_ORIGIN = 'http://127.0.0.1:9010';
const PERMISSION_MODE_API = `${PANEL_ORIGIN}/api/permission-mode`;

export class ChatPermissionStore {
	loaded = $state(false);

	async initialize(): Promise<void> {
		if (this.loaded) return;

		try {
			const rows = await chatPermissionDb.getAll();
			const modes: Record<string, ChatPermissionMode> = {};

			for (const row of rows) modes[row.convId] = row.mode;

			permissionModeStore.replaceChatModes(modes);
		} catch (error) {
			console.error('[chat-permission] could not load per-chat modes:', error);
		}

		// The panel owns the storage, so the store hands every per-chat change
		// back here to be written.
		permissionModeStore.onChatModeChange = (convId, mode) => {
			// An incognito chat's mode lives in memory with the rest of it; writing
			// a row here would outlive the chat that is supposed to leave no trace.
			if (incognitoChatStore.isIncognitoConversation(convId)) return;

			void chatPermissionDb.put({ convId, mode, updatedAt: Date.now() });
		};

		this.loaded = true;
	}

	/**
	 * Keep the panel's global mode file equal to the active chat's mode, since
	 * that file is the only thing the files MCP server can consult.
	 */
	async mirrorActiveChat(): Promise<void> {
		const convId = conversationsStore.activeConversation?.id ?? null;
		const mode = permissionModeStore.getModeFor(convId);

		try {
			const res = await fetch(PERMISSION_MODE_API, {
				body: JSON.stringify({ mode }),
				headers: { 'Content-Type': 'application/json' },
				method: 'POST',
				signal: AbortSignal.timeout(6000)
			});

			if (!res.ok) throw new Error(`status ${res.status}`);
		} catch (error) {
			// Silent by design: this is a best-effort mirror for the files
			// server. A plain llama-server has no panel backend at all, and the
			// in-app gate does not depend on this.
			console.warn('[chat-permission] could not mirror the mode to the panel:', error);
		}
	}
}

export const chatPermissionStore = new ChatPermissionStore();
