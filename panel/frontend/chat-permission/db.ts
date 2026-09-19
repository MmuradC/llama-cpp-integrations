/**
 * Storage for per-chat permission modes.
 *
 * Its own IndexedDB database, like the Projects store and for the same reason:
 * the app's conversation schema (and its Dexie migrations) live inside
 * llama.cpp's tree, so a per-chat field there would be an in-tree edit that an
 * upstream bump could migrate away. Here it is the panel's data, keyed by the
 * app's conversation id.
 *
 * Rows are tiny and written one at a time; a chat with no row inherits the
 * global mode, which is what keeps the feature opt-in.
 */

export const CHAT_PERMISSION_DB = 'LlamaPanelChatState';
export const CHAT_PERMISSION_VERSION = 1;
export const STORE_CHAT_PERMISSIONS = 'permissionModes';

export type ChatPermissionMode = 'manual' | 'accept-edits' | 'auto' | 'plan';

export interface ChatPermissionRow {
	convId: string;
	mode: ChatPermissionMode;
	updatedAt: number;
}

function openDb(): Promise<IDBDatabase> {
	return new Promise((resolve, reject) => {
		const request = indexedDB.open(CHAT_PERMISSION_DB, CHAT_PERMISSION_VERSION);

		request.onupgradeneeded = () => {
			const db = request.result;

			if (!db.objectStoreNames.contains(STORE_CHAT_PERMISSIONS)) {
				db.createObjectStore(STORE_CHAT_PERMISSIONS, { keyPath: 'convId' });
			}
		};

		request.onsuccess = () => resolve(request.result);
		request.onerror = () => reject(request.error ?? new Error('Failed to open chat state db'));
	});
}

export const chatPermissionDb = {
	async getAll(): Promise<ChatPermissionRow[]> {
		const db = await openDb();

		return new Promise((resolve) => {
			const request = db
				.transaction(STORE_CHAT_PERMISSIONS, 'readonly')
				.objectStore(STORE_CHAT_PERMISSIONS)
				.getAll();

			request.onsuccess = () => {
				db.close();
				resolve(request.result as ChatPermissionRow[]);
			};
			request.onerror = () => {
				db.close();
				resolve([]);
			};
		});
	},

	async put(row: ChatPermissionRow): Promise<void> {
		const db = await openDb();

		return new Promise((resolve) => {
			const tx = db.transaction(STORE_CHAT_PERMISSIONS, 'readwrite');

			tx.objectStore(STORE_CHAT_PERMISSIONS).put(row);
			tx.oncomplete = () => {
				db.close();
				resolve();
			};
			tx.onabort = tx.onerror = () => {
				db.close();
				resolve();
			};
		});
	}
};
