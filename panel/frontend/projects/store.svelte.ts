/**
 * projectsStore - the panel's Projects feature.
 *
 * Owns projects, which chat belongs to which project, and the memory
 * proposals waiting for review. All of it lives in the panel's own IndexedDB
 * (see ./db.ts) so nothing here depends on llama.cpp's schema - the app's
 * conversations are only *read* (to list the chats inside a project) and its
 * public stores are only called to apply a project to a new chat.
 */

import { conversationsStore } from '$lib/stores';
import { incognitoChatStore } from '$lib/stores/incognito-chat.svelte';
import {
	loadAll,
	panelDb,
	STORE_ASSIGNMENTS,
	STORE_PROPOSALS,
	STORE_PROJECTS
} from './db';
import type { Assignment, MemoryProposal, Project, ProjectMemory } from './types';

function newId(): string {
	return crypto.randomUUID();
}

export class ProjectsStore {
	projects = $state<Project[]>([]);
	assignments = $state<Assignment[]>([]);
	proposals = $state<MemoryProposal[]>([]);
	loaded = $state(false);
	error = $state('');

	/** Most recently touched first - same ordering the app uses for chats. */
	get orderedProjects(): Project[] {
		return [...this.projects].sort((a, b) => b.lastModified - a.lastModified);
	}

	async initialize(): Promise<void> {
		if (this.loaded) return;

		try {
			const { assignments, projects, proposals } = await loadAll();

			this.projects = projects;
			this.assignments = assignments;
			this.proposals = proposals;
			this.loaded = true;
		} catch (error) {
			this.error = error instanceof Error ? error.message : String(error);
		}
	}

	projectById(projectId: string): Project | null {
		return this.projects.find((p) => p.id === projectId) ?? null;
	}

	/** The project a conversation belongs to, or null for "no project". */
	projectOf(convId: string): Project | null {
		const assignment = this.assignments.find((a) => a.convId === convId);

		return assignment ? this.projectById(assignment.projectId) : null;
	}

	/**
	 * The chats inside a project, newest first - read straight from the app's
	 * conversation list so renames/deletes/reordering stay in one place.
	 */
	chatsOf(projectId: string): DatabaseConversation[] {
		const convIds = new Set(
			this.assignments.filter((a) => a.projectId === projectId).map((a) => a.convId)
		);

		return conversationsStore.conversations
			.filter((c) => convIds.has(c.id))
			.sort((a, b) => b.lastModified - a.lastModified);
	}

	/** Chats that belong to no project. Incognito chats are not listed: they
	 *  are deliberately outside the project system, and offering to file one
	 *  would write a row about a chat that is meant to leave no trace. */
	async chatsWithoutProject(): Promise<DatabaseConversation[]> {
		const assigned = new Set(this.assignments.map((a) => a.convId));

		return conversationsStore.conversations.filter(
			(c) => !assigned.has(c.id) && !incognitoChatStore.isIncognitoConversation(c.id)
		);
	}

	proposalsFor(projectId: string): MemoryProposal[] {
		return this.proposals.filter((p) => p.projectId === projectId);
	}

	async createProject(name: string, patch: Partial<Project> = {}): Promise<Project> {
		const now = Date.now();
		const project: Project = {
			createdAt: now,
			cwd: null,
			id: newId(),
			instructions: '',
			lastModified: now,
			memories: [],
			model: null,
			name: name.trim() || 'Untitled project',
			permissionMode: null,
			...patch
		};

		await panelDb.put(STORE_PROJECTS, project);
		this.projects = [...this.projects, project];

		return project;
	}

	async updateProject(projectId: string, patch: Partial<Project>): Promise<void> {
		const existing = this.projectById(projectId);

		if (!existing) return;

		const updated: Project = { ...existing, ...patch, lastModified: Date.now() };

		await panelDb.put(STORE_PROJECTS, updated);
		this.projects = this.projects.map((p) => (p.id === projectId ? updated : p));
	}

	/**
	 * Deletes a project and keeps its chats: they simply become "no project",
	 * which is recoverable, unlike deleting conversations the user never asked
	 * to lose.
	 */
	async deleteProject(projectId: string): Promise<void> {
		const doomed = this.assignments.filter((a) => a.projectId === projectId);

		await Promise.all([
			panelDb.delete(STORE_PROJECTS, projectId),
			...doomed.map((a) => panelDb.delete(STORE_ASSIGNMENTS, a.convId)),
			...this.proposalsFor(projectId).map((p) => panelDb.delete(STORE_PROPOSALS, p.id))
		]);

		this.projects = this.projects.filter((p) => p.id !== projectId);
		this.assignments = this.assignments.filter((a) => a.projectId !== projectId);
		this.proposals = this.proposals.filter((p) => p.projectId !== projectId);
	}

	/** Put a chat in a project, or pass null to take it out of one. */
	async assign(convId: string, projectId: string | null): Promise<void> {
		if (!projectId) {
			await panelDb.delete(STORE_ASSIGNMENTS, convId);
			this.assignments = this.assignments.filter((a) => a.convId !== convId);

			return;
		}

		const assignment: Assignment = { assignedAt: Date.now(), convId, projectId };

		await panelDb.put(STORE_ASSIGNMENTS, assignment);
		this.assignments = [...this.assignments.filter((a) => a.convId !== convId), assignment];
	}

	async addMemory(projectId: string, text: string, sourceConvId: string | null): Promise<void> {
		const project = this.projectById(projectId);
		const trimmed = text.trim();

		if (!project || !trimmed) return;

		const memory: ProjectMemory = {
			createdAt: Date.now(),
			id: newId(),
			sourceConvId,
			text: trimmed
		};

		await this.updateProject(projectId, { memories: [...project.memories, memory] });
	}

	async removeMemory(projectId: string, memoryId: string): Promise<void> {
		const project = this.projectById(projectId);

		if (!project) return;

		await this.updateProject(projectId, {
			memories: project.memories.filter((m) => m.id !== memoryId)
		});
	}

	/** Store model-suggested memories as proposals; nothing is remembered until approved. */
	async addProposals(projectId: string, convId: string, texts: string[]): Promise<void> {
		const project = this.projectById(projectId);

		if (!project) return;

		const known = new Set(project.memories.map((m) => m.text.toLowerCase()));
		const already = new Set(
			this.proposalsFor(projectId).map((p) => p.text.toLowerCase())
		);
		const fresh = texts
			.map((t) => t.trim())
			.filter((t) => t.length > 0)
			.filter((t) => !known.has(t.toLowerCase()) && !already.has(t.toLowerCase()));

		if (fresh.length === 0) return;

		const proposals: MemoryProposal[] = fresh.map((text) => ({
			convId,
			createdAt: Date.now(),
			id: newId(),
			projectId,
			text
		}));

		await Promise.all(proposals.map((p) => panelDb.put(STORE_PROPOSALS, p)));
		this.proposals = [...this.proposals, ...proposals];
	}

	async approveProposal(proposalId: string): Promise<void> {
		const proposal = this.proposals.find((p) => p.id === proposalId);

		if (!proposal) return;

		await this.addMemory(proposal.projectId, proposal.text, proposal.convId);
		await this.rejectProposal(proposalId);
	}

	async rejectProposal(proposalId: string): Promise<void> {
		await panelDb.delete(STORE_PROPOSALS, proposalId);
		this.proposals = this.proposals.filter((p) => p.id !== proposalId);
	}

	async approveAll(projectId: string): Promise<void> {
		for (const proposal of this.proposalsFor(projectId)) {
			await this.approveProposal(proposal.id);
		}
	}

	async rejectAll(projectId: string): Promise<void> {
		for (const proposal of this.proposalsFor(projectId)) {
			await this.rejectProposal(proposal.id);
		}
	}
}

export const projectsStore = new ProjectsStore();
