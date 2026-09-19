/**
 * Applying a project to a chat.
 *
 * Only public app APIs are used (no in-tree changes): the model picker, the
 * new-chat cwd threading, the permission mode store, and the app's own
 * database service for the system message.
 *
 * The project's instructions and memories go into the *chat's own* system
 * message rather than the app's global system-message setting, so a chat with
 * no project - or in another project - is unaffected, and moving a chat
 * between projects never rewrites a global that other chats read.
 */

import { MessageRole } from '$lib/enums';
import { DatabaseService } from '$lib/services/database.service';
import { conversationsStore, modelsStore, permissionModeStore } from '$lib/stores';
import { projectsStore } from './store.svelte';
import type { Project } from './types';

/** Instructions first, then memories, in the order the model should read them. */
export function buildProjectPrompt(project: Project): string {
	const sections: string[] = [];
	const instructions = project.instructions.trim();

	if (instructions) sections.push(instructions);

	if (project.memories.length > 0) {
		sections.push(
			[
				'Things to remember about this project:',
				...project.memories.map((memory) => `- ${memory.text}`)
			].join('\n')
		);
	}

	return sections.join('\n\n');
}

/**
 * Put the text into `convId`'s own system message and make sure it is on the
 * active path, so the request actually carries it.
 *
 * A conversation that already has a system message gets it updated in place;
 * a fresh one gets a new system message under its root, and the current node
 * pointer moves to it. That second step matters: history is rebuilt by walking
 * from the current node up to the root, so a system message that is not an
 * ancestor of the current node would simply never be sent.
 */
async function writeProjectSystemMessage(convId: string, text: string): Promise<boolean> {
	const messages = await conversationsStore.getConversationMessages(convId);
	const existing = messages.find((m) => m.role === MessageRole.SYSTEM);

	if (existing) {
		await DatabaseService.updateMessage(existing.id, { content: text });

		const updated = { ...existing, content: text };

		if (!conversationsStore.activeMessages.some((m) => m.id === existing.id)) {
			conversationsStore.addMessageToActive(updated);
		}

		return true;
	}

	const existingRoot = messages.find((m) => m.type === 'root' && m.parent === null);
	const rootId = existingRoot?.id ?? (await DatabaseService.createRootMessage(convId));
	const systemMessage = await DatabaseService.createSystemMessage(convId, text, rootId);

	conversationsStore.addMessageToActive(systemMessage);
	await conversationsStore.updateCurrentNode(systemMessage.id);

	return true;
}

export interface AppliedProject {
	convId: string;
	/** Human-readable list of what was applied, for the confirmation toast. */
	applied: string[];
}

/**
 * Start a new chat inside a project: its model, working directory, permission
 * mode, instructions and memories, then record the chat as belonging to it.
 */
export async function startChatInProject(project: Project): Promise<AppliedProject> {
	const applied: string[] = [];

	if (project.permissionMode) {
		permissionModeStore.setMode(project.permissionMode);
		applied.push(`permission mode ${project.permissionMode}`);
	}

	if (project.model) {
		modelsStore.selectModelByName(project.model);
		applied.push(`model ${project.model}`);
	}

	// createConversation threads pendingCwd into the new conversation and
	// clears it afterwards, which is exactly the app's own new-chat path.
	conversationsStore.preferences.pendingCwd = project.cwd ?? null;

	if (project.cwd) applied.push(`working directory ${project.cwd}`);

	const convId = await conversationsStore.createConversation(project.name);

	const prompt = buildProjectPrompt(project);

	if (prompt) {
		await writeProjectSystemMessage(convId, prompt);
		applied.push(
			project.memories.length > 0
				? `instructions + ${project.memories.length} memor${project.memories.length === 1 ? 'y' : 'ies'}`
				: 'instructions'
		);
	}

	await projectsStore.assign(convId, project.id);

	return { applied, convId };
}

/**
 * Re-apply a project to the chat that is already open - the explicit "apply
 * project settings" action. Same pieces as above except the model and cwd,
 * which only make sense at creation time: changing the open chat's model or
 * working directory mid-conversation is a decision for the user, not a side
 * effect of pressing apply.
 */
export async function applyProjectToCurrentChat(project: Project): Promise<AppliedProject> {
	const convId = conversationsStore.activeConversation?.id;

	if (!convId) throw new Error('No conversation is open');

	const applied: string[] = [];

	if (project.permissionMode) {
		permissionModeStore.setMode(project.permissionMode);
		applied.push(`permission mode ${project.permissionMode}`);
	}

	const prompt = buildProjectPrompt(project);

	if (prompt) {
		await writeProjectSystemMessage(convId, prompt);
		applied.push('instructions');
	}

	await projectsStore.assign(convId, project.id);

	return { applied, convId };
}
