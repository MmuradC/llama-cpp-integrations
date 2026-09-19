/**
 * Incognito chats - panel-side entry point.
 *
 * The mechanism itself lives in the app's tree (lib/stores/incognito-chat and
 * the guards inside DatabaseService), because it has to intercept persistence
 * where it happens; this is just the one way to start such a chat, shared by
 * the `/incognito` command and the panel's button so they cannot drift.
 */

import { conversationsStore } from '$lib/stores';
import { incognitoChatStore } from '$lib/stores/incognito-chat.svelte';

export const INCOGNITO_SUMMARY =
	'Nothing in this chat is written to disk — no history, no messages, no trace on reload. It is gone when you leave or close the tab.';

/**
 * Ask for the next conversation to be incognito and open a fresh chat.
 *
 * The flag is consumed the moment a conversation is created (see
 * DatabaseService.createConversation), so it applies to the chat the user is
 * about to start and to nothing else.
 */
export async function startIncognitoChat(): Promise<void> {
	incognitoChatStore.requestNext();
	await conversationsStore.openNewChat();
}
