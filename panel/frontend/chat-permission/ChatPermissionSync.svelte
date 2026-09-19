<script lang="ts">
	// No UI. This exists to run the two side effects per-chat permission modes
	// need while the app is open; see ./store.svelte.ts for why.
	//
	// Rendered by RightBar.svelte *outside* its expanded/collapsed branch, so it
	// is mounted whether or not the panel is open.
	import { conversationsStore } from '$lib/stores';
	import { incognitoChatStore } from '$lib/stores/incognito-chat.svelte';
	import { permissionModeStore } from '$lib/stores/permission-mode.svelte';
	import { chatPermissionStore } from './store.svelte';

	// Load the per-chat modes and hand the app a place to write changes back to.
	$effect(() => {
		void chatPermissionStore.initialize();
	});

	// Keep the panel's global mode file equal to the active chat's mode: that
	// file is the only thing the files MCP server can consult, and a tool call
	// reaching it carries no conversation id.
	$effect(() => {
		const convId = conversationsStore.activeConversation?.id ?? null;
		const mode = permissionModeStore.getModeFor(convId);

		void mode;
		void chatPermissionStore.mirrorActiveChat();
	});

	// A pending incognito request belongs to the chat the user is about to
	// start. Opening some other chat first must not turn that chat incognito,
	// so drop the request when a real conversation becomes active - the
	// creation path consumes it before that happens.
	$effect(() => {
		const convId = conversationsStore.activeConversation?.id ?? null;

		if (!convId) return;

		queueMicrotask(() => incognitoChatStore.cancelPending());
	});
</script>
