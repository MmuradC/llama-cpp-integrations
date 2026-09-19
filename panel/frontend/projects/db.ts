/**
 * Storage for the Projects feature: its own IndexedDB database, deliberately
 * separate from the app's `LlamaUi` database.
 *
 * Why separate: `LlamaUi`'s schema is declared and migrated inside llama.cpp
 * (lib/constants/database.constants.ts + Dexie versions), so adding tables
 * there would mean editing the app's tree - and an upstream version bump could
 * migrate or drop them. This database is opened and owned by the panel alone,
 * so a llama.cpp update cannot see it, let alone touch it.
 *
 * Plain promisified IndexedDB, no Dexie: the panel must not depend on a
 * package the app happens to install (a bare specifier here would not even
 * resolve - see RightBar.svelte's note on that), and three small stores do not
 * need an ORM.
 */

import type { Assignment, MemoryProposal, Project } from './types';

export const PANEL_DB_NAME = 'LlamaPanelProjects';
export const PANEL_DB_VERSION = 1;

export const STORE_PROJECTS = 'projects';
export const STORE_ASSIGNMENTS = 'assignments';
export const STORE_PROPOSALS = 'proposals';

function openDb(): Promise<IDBDatabase> {
	return new Promise((resolve, reject) => {
		const request = indexedDB.open(PANEL_DB_NAME, PANEL_DB_VERSION);

		request.onupgradeneeded = () => {
			const db = request.result;

			if (!db.objectStoreNames.contains(STORE_PROJECTS)) {
				db.createObjectStore(STORE_PROJECTS, { keyPath: 'id' });
			}

			if (!db.objectStoreNames.contains(STORE_ASSIGNMENTS)) {
				db.createObjectStore(STORE_ASSIGNMENTS, { keyPath: 'convId' });
			}

			if (!db.objectStoreNames.contains(STORE_PROPOSALS)) {
				db.createObjectStore(STORE_PROPOSALS, { keyPath: 'id' });
			}
		};

		request.onsuccess = () => resolve(request.result);
		request.onerror = () => reject(request.error ?? new Error('Failed to open panel database'));
	});
}

function run<T>(
	store: string,
	mode: IDBTransactionMode,
	work: (objectStore: IDBObjectStore) => IDBRequest<T>
): Promise<T> {
	return openDb().then(
		(db) =>
			new Promise<T>((resolve, reject) => {
				const tx = db.transaction(store, mode);
				const request = work(tx.objectStore(store));

				request.onsuccess = () => resolve(request.result);
				request.onerror = () => reject(request.error ?? new Error('IndexedDB request failed'));

				tx.oncomplete = () => db.close();
				tx.onabort = () => {
					db.close();
					reject(tx.error ?? new Error('IndexedDB transaction aborted'));
				};
			})
	);
}

export const panelDb = {
	getAll<T>(store: string): Promise<T[]> {
		return run<T[]>(store, 'readonly', (s) => s.getAll() as IDBRequest<T[]>);
	},
	put<T>(store: string, value: T): Promise<IDBValidKey> {
		return run<IDBValidKey>(store, 'readwrite', (s) => s.put(value));
	},
	delete(store: string, key: string): Promise<undefined> {
		return run<undefined>(store, 'readwrite', (s) => s.delete(key) as IDBRequest<undefined>);
	},
	clear(store: string): Promise<undefined> {
		return run<undefined>(store, 'readwrite', (s) => s.clear() as IDBRequest<undefined>);
	}
};

/** Load everything the store needs in one go. */
export async function loadAll(): Promise<{
	assignments: Assignment[];
	projects: Project[];
	proposals: MemoryProposal[];
}> {
	const [projects, assignments, proposals] = await Promise.all([
		panelDb.getAll<Project>(STORE_PROJECTS),
		panelDb.getAll<Assignment>(STORE_ASSIGNMENTS),
		panelDb.getAll<MemoryProposal>(STORE_PROPOSALS)
	]);

	return { assignments, projects, proposals };
}
