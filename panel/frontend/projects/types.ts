/**
 * Types for the panel's Projects feature.
 *
 * Lives in panel/frontend, not in llama.cpp's tree: the whole point is that a
 * llama.cpp version bump cannot take Projects away, and that the app's own
 * schema (which upstream versions and migrates) is never touched.
 */

export type ProjectPermissionMode = 'manual' | 'accept-edits' | 'auto' | 'plan';

/** One durable fact attached to a project. Written by the user, or approved from a model proposal. */
export interface ProjectMemory {
	id: string;
	text: string;
	createdAt: number;
	/** Conversation the memory came from, when it was proposed by the model. */
	sourceConvId: string | null;
}

export interface Project {
	id: string;
	name: string;
	/** Free-form instructions, prepended to the system prompt of chats in this project. */
	instructions: string;
	memories: ProjectMemory[];
	/** Working directory new chats in this project start in. */
	cwd: string | null;
	/** Model id new chats in this project select. */
	model: string | null;
	/** Permission mode new chats in this project start in. */
	permissionMode: ProjectPermissionMode | null;
	createdAt: number;
	lastModified: number;
}

/** Which project a conversation belongs to. Absent row means "no project". */
export interface Assignment {
	convId: string;
	projectId: string;
	assignedAt: number;
}

/** A memory the model suggested after a chat; waits for the user to approve or reject it. */
export interface MemoryProposal {
	id: string;
	projectId: string;
	convId: string;
	text: string;
	createdAt: number;
}
