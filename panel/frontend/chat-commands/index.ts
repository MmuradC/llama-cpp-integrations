/**
 * Slash commands contributed by the panel.
 *
 * The WebUI already ships a `/` picker with three commands of its own
 * (`/model`, `/cwd`, `/prompt`) — the registry in
 * `tools/ui/src/lib/utils/chat-commands.ts`. This module adds the panel's own
 * commands to that same picker; it does not reimplement any of the picker
 * machinery (filtering, keyboard navigation, dismiss handling).
 *
 * How the wiring works, and why it is shaped like this:
 *
 *   - `getPanelCommands()` returns command *descriptors* (name, description,
 *     keywords, icon, availability) which the in-tree registry appends to its
 *     own three behind one extra `ChatFormCommandAction.PANEL` action.
 *   - `runPanelCommand()` is what the in-tree dispatcher calls when one of
 *     them is picked; it looks the name up here and runs the real body.
 *   - Because every body lives outside the llama.cpp tree, adding or changing
 *     a command never touches a patch file.
 *
 * Availability is expressed as per-command `disabled` predicates rather than a
 * flat options bag, so it can read the stores directly. `getPanelCommands()`
 * is called from inside a `$derived` in the in-tree hook, so any store read
 * inside a predicate makes the picker re-evaluate when that store changes —
 * `/stop` greys out the moment generation finishes, `/copy` the moment an
 * assistant reply appears.
 *
 * Aliases: a command is only dispatched when picked from the list (the
 * in-tree design deliberately never acts on typed text mid-typing), so an
 * alias only has to *find* the row — which is why aliases are keywords
 * rather than duplicate rows. `/new` and `/reset` both surface `/clear`.
 *
 * Two of these commands are written against surfaces that differ between
 * llama.cpp versions, so both cases are handled explicitly rather than
 * assumed:
 *
 *   - `/settings`, `/theme` and `/mcp` navigate to the app's own hash routes
 *     (`#/settings/general`, `#/mcp-servers`) — the same destinations the
 *     sidebar's own icon strip uses (see ui.constants.ts). Newer UI builds
 *     render settings and MCP as in-app views driven by the URL hash; older
 *     ones render dialogs, in which case these three would need the dialog
 *     seam instead (see panel/patches/README.md).
 *   - `/compact` is a *panel* feature, not an upstream one: it exists only
 *     where that patch's `chatStore.compactConversation` is present. Rather
 *     than statically importing it (which would fail to build in a tree
 *     without it) the command probes for it at runtime and stays disabled
 *     when it is absent.
 */

import { browser } from '$app/environment';
import { goto } from '$app/navigation';
import {
	COMPACTION_CHARS_PER_CONTEXT_TOKEN,
	DEFAULT_COMPACTION_CONTEXT_TOKENS,
	PERMISSION_MODE_LABELS,
	ROUTES
} from '$lib/constants';
import { ChatFormCommandAction, MessageRole } from '$lib/enums';
import { RouterService } from '$lib/services/router.service';
import {
	chatStore,
	contextStatsStore,
	conversationsStore,
	modelsStore,
	permissionModeStore,
	settingsStore,
	uiStore
} from '$lib/stores';
import { incognitoChatStore } from '$lib/stores/incognito-chat.svelte';
import { INCOGNITO_SUMMARY, startIncognitoChat } from '../incognito';
import type { PermissionMode } from '$lib/types/agentic';
import type { ChatFormCommand } from '$lib/types';
import { copyToClipboard, formatMessageForClipboard } from '$lib/utils';
// Icons and the toast helper come from an in-tree shim rather than straight
// from `@lucide/svelte` and `svelte-sonner`: this file lives outside tools/ui,
// so a bare package specifier would resolve node_modules relative to its own
// path and never find tools/ui/node_modules. Exactly the constraint
// RightBar.svelte documents for its icons; see
// tools/ui/src/lib/utils/panel-command-runtime.ts, added by this patch.
import {
	Activity,
	CircleQuestionMark,
	Copy,
	Download,
	EyeOff,
	Gauge,
	GitBranch,
	History,
	PackageOpen,
	Palette,
	PencilLine,
	Plug,
	Receipt,
	Settings,
	ShieldCheck,
	Square,
	SquarePen,
	toast
} from '$lib/utils/panel-command-runtime';
import type { Component } from 'svelte';

/** Origin of panel/backend/server.py — the same constant RightBar.svelte uses. */
const PANEL_ORIGIN = 'http://127.0.0.1:9010';

/** The backend's HTML usage pages, keyed by the model-id provider prefix. */
const USAGE_PAGES: Record<string, string> = {
	nim: '/nim-usage',
	opencode: '/opencode-usage',
	openrouter: '/openrouter-usage'
};

/** Where the app's own settings entry points; holds the Theme control. */
const SETTINGS_GENERAL = `${ROUTES.SETTINGS}/general`;

/**
 * The context gauge's dial. The popup positions its card from the trigger
 * element's own bounding box, so this is how `/context` finds the thing to
 * hand to the popup — see the `run` body for why it synthesises events
 * instead of just flipping the popup's shared state.
 *
 * Upstream attribute, not one we add: no patch hunk is needed for it.
 */
const CONTEXT_GAUGE_SELECTOR = '[data-context-gauge-trigger]';

/** How long `/status` waits for the panel backend before giving up. */
const DASHBOARD_TIMEOUT_MS = 4000;

type CommandHandler = (args: string) => void | Promise<void>;

interface PanelCommand {
	name: string;
	description: string;
	/** Search terms: aliases and synonyms, matched by the picker's filter. */
	keywords?: string[];
	icon: Component;
	/** Evaluated by the picker each time it opens. */
	disabled: () => boolean;
	run: CommandHandler;
}

interface PanelDashboard {
	llama_server: { loaded?: string[]; reachable: boolean };
	sd_server: { model?: string | null; reachable: boolean };
}

/** The shape `/compact` needs, probed at runtime — see the module docstring. */
type CompactRunner = () => Promise<{ error?: string; ok: boolean }>;

/** The conversation the chat form is showing, if any. */
function activeConversationId(): string | null {
	return conversationsStore.activeConversation?.id ?? null;
}

/** The conversation's most recent assistant reply, if it has one. */
function lastAssistantMessage(): DatabaseMessage | null {
	const messages = conversationsStore.activeMessages;

	for (let i = messages.length - 1; i >= 0; i--) {
		if (messages[i].role === MessageRole.ASSISTANT) return messages[i];
	}

	return null;
}

/** Whether the active conversation has a response in flight. */
function isGenerating(): boolean {
	const id = activeConversationId();

	if (!id) return false;
	if (chatStore.getChatStreaming(id) !== undefined) return true;

	return chatStore.getAllLoadingChats().includes(id);
}

/**
 * `chatStore.compactConversation`, where the compaction patch provides it.
 *
 * Accessed through a cast rather than directly: the method does not exist in
 * every tree this panel runs against, and a plain property access on a class
 * instance whose type lacks it is a compile error — which would make this
 * whole module unbuildable there, taking every other command down with it.
 */
function compactRunner(): CompactRunner | undefined {
	return (chatStore as unknown as { compactConversation?: CompactRunner })
		.compactConversation;
}

/**
 * Provider of the model currently selected in the picker, derived from the
 * registered id. The panel backend registers remote models as
 * `f"{provider}/" + real_id.replace("/", "__")` (see `_registered_id` in
 * panel/backend/server.py), so the prefix is the provider — local models
 * carry no known prefix and answer null, which is why `/usage` also takes an
 * explicit provider argument.
 */
function currentProvider(): string | null {
	const id = modelsStore.selectedModelId ?? '';
	const prefix = id.includes('/') ? (id.split('/')[0] ?? '') : '';

	return prefix in USAGE_PAGES ? prefix : null;
}

/** `/status`'s view of the panel backend; null when it cannot be reached. */
async function fetchDashboard(): Promise<PanelDashboard | null> {
	try {
		const res = await fetch(`${PANEL_ORIGIN}/api/dashboard`, {
			signal: AbortSignal.timeout(DASHBOARD_TIMEOUT_MS)
		});

		if (!res.ok) return null;

		return (await res.json()) as PanelDashboard;
	} catch {
		// The chat works with the panel backend down; `/status` reports that
		// rather than throwing a fetch error into the void.
		return null;
	}
}

/**
 * Every panel command, in the order the picker lists them.
 *
 * The three the WebUI already owns (`/model`, `/cwd`, `/prompt`) are not
 * repeated here — the in-tree registry lists those first, then appends these.
 */
const COMMANDS: PanelCommand[] = [
	{
		description: 'Start a new conversation',
		disabled: () => false,
		icon: SquarePen,
		keywords: ['new', 'reset'],
		name: 'clear',
		run: async () => {
			await conversationsStore.openNewChat();
		}
	},
	{
		description: 'Start a chat that is never saved',
		disabled: () => false,
		icon: EyeOff,
		keywords: ['private', 'temporary', 'no history', 'ephemeral'],
		name: 'incognito',
		run: async () => {
			await startIncognitoChat();
			toast.message('Incognito chat', { description: INCOGNITO_SUMMARY });
		}
	},
	{
		description: 'Summarize the conversation to free up its context',
		// Nothing to summarize while a response is still streaming into it.
		disabled: () =>
			!compactRunner() ||
			!activeConversationId() ||
			conversationsStore.activeMessages.length === 0 ||
			isGenerating(),
		icon: PackageOpen,
		keywords: ['summarize', 'condense', 'free space'],
		name: 'compact',
		run: async () => {
			const compact = compactRunner();

			if (!compact) {
				// Only reachable if the command is dispatched without the
				// picker's disabled gate, e.g. by a stale open menu.
				toast.error('Compaction is not available in this build');

				return;
			}

			// Compaction is one or more summarization calls, and the first may also
			// have to load the model. On a large model, or a long conversation,
			// that is minutes with nothing changing on screen, which reads as "the
			// command is stuck". So: show a loading toast immediately, and state
			// the real work up front (how many messages, roughly how many calls)
			// so waiting is legible. The same toast id later turns it into the
			// result instead of stacking a second toast.
			//
			// The estimate mirrors chatStore.summarizeTranscript's own batching:
			// the transcript is the user+assistant turns joined, and the budget is
			// the model's context size times COMPACTION_CHARS_PER_CONTEXT_TOKEN,
			// falling back to DEFAULT_COMPACTION_CONTEXT_TOKENS when nothing
			// client-side knows the context size (which is every remote model).
			const compactable = conversationsStore.activeMessages.filter(
				(m) => m.role === MessageRole.USER || m.role === MessageRole.ASSISTANT
			);
			const transcript = compactable.map(
				(m) => `${m.role === MessageRole.USER ? 'User' : 'Assistant'}: ${m.content}`
			);
			const transcriptChars = transcript.join('\n\n').length;
			const compactModel =
				modelsStore.selectedModelName ?? modelsStore.activeModelId ?? null;
			// `||` mirrors summarizeTranscript: the server reports n_ctx 0 for
			// remote models and getModelContextSize returns that 0 as a number,
			// so `??` would let a 0 through and divide by zero here.
			const contextTokens =
				modelsStore.props.getModelContextSize(compactModel ?? '') ||
				DEFAULT_COMPACTION_CONTEXT_TOKENS;
			const budget = Math.max(4096, contextTokens * COMPACTION_CHARS_PER_CONTEXT_TOKEN);
			const batches = Math.max(1, Math.ceil(transcriptChars / budget));
			const toastId = 'panel-command-compact';

			toast.loading('Compacting conversation…', {
				description: `Summarizing ${compactable.length} messages (~${Math.round(
					transcriptChars / 1000
				)}k characters) with ${compactModel ?? 'the selected model'} — about ${batches} call${
					batches === 1 ? '' : 's'
				} to the model. Nothing is deleted, and reloading the page cancels it.`,
				duration: Number.POSITIVE_INFINITY,
				id: toastId
			});

			try {
				const result = await compact.call(chatStore);

				if (result.ok) {
					toast.success('Conversation compacted', { id: toastId });
				} else {
					toast.error(result.error ?? 'Failed to compact the conversation', { id: toastId });
				}
			} catch (error) {
				toast.error(
					`Compaction failed: ${error instanceof Error ? error.message : String(error)}`,
					{ id: toastId }
				);
			}
		}
	},
	{
		description: 'Show what is filling the context window',
		disabled: () => !browser || document.querySelector(CONTEXT_GAUGE_SELECTOR) === null,
		icon: Gauge,
		keywords: ['tokens', 'window', 'gauge'],
		name: 'context',
		run: () => {
			const trigger = document.querySelector<HTMLElement>(CONTEXT_GAUGE_SELECTOR);

			if (!trigger) {
				// The gauge hides itself unless a model is loaded AND there is a
				// context to measure, and the usual reason it is missing is simply
				// that no model is loaded - the router unloads idle models. Say
				// which of the two it is instead of a flat "not on screen".
				toast.error(
					contextStatsStore.isActiveModelLoaded
						? 'The context gauge is not on screen'
						: 'No model is loaded, so there is no context to show yet — send a message (or pick a model) to load one'
				);

				return;
			}

			// The gauge only opens from its own pointer events, and it places
			// the card relative to the trigger's box — setting `gaugePopup.open`
			// directly would open an unpositioned card in the corner. So send
			// the events its handlers already listen for: `pointerdown` tells it
			// the pointer is a touch (which makes the click a toggle rather than
			// a hover-open), then the click toggles the card. Tapping it twice
			// therefore closes it again, matching the dial's own behaviour.
			trigger.dispatchEvent(new PointerEvent('pointerdown', { pointerType: 'touch' }));
			trigger.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
		}
	},
	{
		description: 'Copy the latest assistant reply',
		disabled: () => lastAssistantMessage() === null,
		icon: Copy,
		keywords: ['clipboard'],
		name: 'copy',
		run: async () => {
			const message = lastAssistantMessage();

			if (!message) {
				toast.error('There is no assistant reply to copy yet');

				return;
			}

			// Same conversion the per-message copy button uses, so attachments
			// and MCP content come across identically.
			await copyToClipboard(
				formatMessageForClipboard(
					message.content,
					message.extra,
					Boolean(settingsStore.config.copyTextAttachmentsAsPlainText)
				),
				'Message copied to clipboard'
			);
		}
	},
	{
		description: 'Stop the response currently being generated',
		disabled: () => !isGenerating(),
		icon: Square,
		keywords: ['halt', 'abort', 'cancel'],
		name: 'stop',
		run: async () => {
			await chatStore.stopGeneration();
		}
	},
	{
		description: 'Rename the current conversation',
		disabled: () => !activeConversationId(),
		icon: PencilLine,
		keywords: ['title', 'name'],
		name: 'rename',
		run: async (args) => {
			const conversation = conversationsStore.activeConversation;

			if (!conversation) {
				toast.error('No conversation is open');

				return;
			}

			const name = args.trim();

			if (!name) {
				toast.error('Type the new name after the command, e.g. /rename Tokenizer notes');

				return;
			}

			await conversationsStore.updateConversationName(conversation.id, name);
			toast.success(`Renamed to "${name}"`);
		}
	},
	{
		description: 'Download this conversation as a file',
		disabled: () => !activeConversationId(),
		icon: Download,
		keywords: ['save', 'download', 'json'],
		name: 'export',
		run: async () => {
			const id = activeConversationId();

			if (!id) {
				toast.error('No conversation is open');

				return;
			}

			await conversationsStore.downloadConversation(id);
		}
	},
	{
		description: 'Fork this conversation from its last message',
		disabled: () =>
			!activeConversationId() || conversationsStore.activeMessages.length === 0,
		icon: GitBranch,
		keywords: ['fork', 'duplicate', 'try again'],
		name: 'branch',
		run: async () => {
			const conversation = conversationsStore.activeConversation;
			const messages = conversationsStore.activeMessages;
			const last = messages[messages.length - 1];

			if (!conversation || !last) {
				toast.error('There is nothing to branch yet');

				return;
			}

			// Forks navigate to the copy and toast their own result, so there
			// is nothing to report here. Attachments ride along: a branch that
			// silently lost the files the conversation was about would be worse
			// than the space it costs.
			await conversationsStore.forkConversation(last.id, {
				includeAttachments: true,
				name: `${conversation.name} (branch)`
			});
		}
	},
	{
		description: 'Reopen an earlier conversation by name',
		disabled: () => conversationsStore.conversations.length === 0,
		icon: History,
		keywords: ['history', 'continue', 'load', 'open'],
		name: 'resume',
		run: async (args) => {
			const query = args.trim().toLowerCase();

			if (!query) {
				// The sidebar's search is the real conversation browser here, so
				// open it and say what to type rather than guessing a target.
				uiStore.isSidebarExpanded = true;
				toast.message('Reopen a conversation', {
					description:
						'Type a name to go straight there, e.g. /resume tokenizer — or pick from the sidebar, which is now open.'
				});

				return;
			}

			const all = conversationsStore.conversations;
			const match =
				all.find((c) => c.name.toLowerCase() === query) ??
				all.find((c) => c.name.toLowerCase().includes(query));

			if (!match) {
				toast.error(`No conversation matching "${args.trim()}"`);

				return;
			}

			await goto(RouterService.chat(match.id));
			toast.success(`Opened "${match.name}"`);
		}
	},
	{
		description: 'Provider spend and rate limits',
		disabled: () => false,
		icon: Receipt,
		keywords: ['cost', 'stats', 'credits', 'spend', 'limits'],
		name: 'usage',
		run: (args) => {
			// Explicit argument wins; otherwise infer the provider from the
			// selected model's registered id.
			const requested = args.trim().toLowerCase();
			const provider = requested || currentProvider();
			const page = provider ? USAGE_PAGES[provider] : undefined;

			if (!page) {
				toast.error(
					`Which provider? ${Object.keys(USAGE_PAGES)
						.map((p) => `/usage ${p}`)
						.join(', ')}`
				);

				return;
			}

			window.open(`${PANEL_ORIGIN}${page}`, '_blank', 'noopener');
		}
	},
	{
		description: 'Version, model and backend status',
		disabled: () => false,
		icon: Activity,
		keywords: ['info', 'health', 'diagnostics'],
		name: 'status',
		run: async () => {
			const model = modelsStore.selectedModelName ?? 'none selected';
			const lines = [`model ${model}`];
			const dashboard = await fetchDashboard();

			if (dashboard) {
				lines.push(`llama-server ${dashboard.llama_server.reachable ? 'up' : 'down'}`);

				if (dashboard.llama_server.loaded?.length) {
					lines.push(`loaded ${dashboard.llama_server.loaded.join(', ')}`);
				}

				if (dashboard.sd_server.reachable) {
					lines.push(`image server ${dashboard.sd_server.model ?? 'up'}`);
				}
			} else {
				lines.push('panel backend unreachable');
			}

			// Joined rather than newline-separated: sonner renders the
			// description as a single paragraph, so newlines collapse anyway.
			toast.message('Status', { description: lines.join(' · '), duration: 10000 });
		}
	},
	{
		description: 'Open settings',
		disabled: () => false,
		icon: Settings,
		keywords: ['config', 'preferences', 'options'],
		name: 'settings',
		run: async () => {
			await goto(SETTINGS_GENERAL);
		}
	},
	{
		description: 'Color theme (opens Settings → General)',
		disabled: () => false,
		icon: Palette,
		keywords: ['dark', 'light', 'appearance', 'color'],
		name: 'theme',
		run: async () => {
			// Theme lives in the General section of SETTINGS_REGISTRY, which is
			// also where the app's own sidebar entry points.
			await goto(SETTINGS_GENERAL);
		}
	},
	{
		description: 'MCP servers',
		disabled: () => false,
		icon: Plug,
		keywords: ['tools', 'servers'],
		name: 'mcp',
		run: async () => {
			await goto(ROUTES.MCP_SERVERS);
		}
	},
	{
		description: 'Tool approval mode',
		disabled: () => false,
		icon: ShieldCheck,
		keywords: ['manual', 'auto', 'accept edits', 'plan', 'gates', 'approval'],
		name: 'permissions',
		run: (args) => {
			const modes = Object.keys(PERMISSION_MODE_LABELS) as PermissionMode[];
			// `/permissions accept edits` as well as the canonical
			// `accept-edits`: typed arguments pick up prose spacing.
			const requested = args.trim().toLowerCase().replace(/\s+/g, '-');

			if (!requested) {
				const current = permissionModeStore.mode;

				toast.message('Permission mode', {
					description: `Currently "${PERMISSION_MODE_LABELS[current]}". Change it with ${modes
						.map((m) => `/permissions ${m}`)
						.join(', ')}.`
				});

				return;
			}

			const match =
				modes.find((m) => m === requested) ??
				modes.find((m) => m.startsWith(requested));

			if (!match) {
				toast.error(`Unknown mode "${args.trim()}" — try ${modes.join(', ')}`);

				return;
			}

			permissionModeStore.setMode(match);
			toast.success(`Permission mode: ${PERMISSION_MODE_LABELS[match]}`);
		}
	},
	{
		description: 'List the available slash commands',
		disabled: () => false,
		icon: CircleQuestionMark,
		keywords: ['commands', 'what can you do'],
		name: 'help',
		run: () => {
			toast.message('Slash commands', {
				description: COMMANDS.map((c) => `/${c.name}`).join(' · '),
				duration: 15000
			});
		}
	}
];

/**
 * The panel's commands, in the shape the WebUI's registry expects. Every one
 * of them carries `ChatFormCommandAction.PANEL`; the name is what the
 * dispatcher routes on, so the picker's own filtering still matches on name,
 * description and keywords.
 *
 * Called from a `$derived` in the in-tree hook, which is what makes the
 * `disabled` predicates reactive — they are evaluated here, on every
 * re-evaluation, rather than stored.
 */
export function getPanelCommands(): ChatFormCommand[] {
	return COMMANDS.map((command) => ({
		action: ChatFormCommandAction.PANEL,
		description: command.description,
		disabled: command.disabled(),
		icon: command.icon,
		keywords: command.keywords,
		name: command.name
	}));
}

/**
 * Run a panel command by name — the other half of the seam, called by the
 * in-tree dispatcher when `ChatFormCommandAction.PANEL` is selected.
 *
 * Handlers are not awaited by the caller (the picker closes and the input
 * clears regardless), so failures are caught here and surfaced as a toast:
 * an unhandled rejection from a command body would otherwise be invisible.
 */
export function runPanelCommand(name: string, args: string): void {
	const command = COMMANDS.find((c) => c.name === name);

	if (!command) {
		toast.error(`Unknown command /${name}`);

		return;
	}

	void Promise.resolve(command.run(args)).catch((error: unknown) => {
		const detail = error instanceof Error ? error.message : String(error);

		console.error(`/${name} failed`, error);
		toast.error(`/${name} failed: ${detail}`);
	});
}
