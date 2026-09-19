<script lang="ts">
	// Projects: a list of projects, each holding its own chats, rendered in the
	// panel's right bar. Everything lives in panel/frontend (see ../README.md
	// and store.svelte.ts) - llama.cpp's tree is only read through its public
	// stores, never modified, so an app update cannot remove this.
	import { goto } from '$app/navigation';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import * as Collapsible from '$lib/components/ui/collapsible';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu';
	import { RouterService } from '$lib/services/router.service';
	import { toast } from '$lib/utils/panel-command-runtime';
	import {
		ChevronRight,
		Folder,
		FolderPlus,
		Pencil,
		Plus,
		Trash2
	} from '$lib/utils/panel-command-runtime';
	import ProjectEditor from './ProjectEditor.svelte';
	import { startChatInProject } from './apply';
	import { projectsStore } from './store.svelte';
	import type { Project } from './types';

	let editorOpen = $state(false);
	let editorProject = $state<Project | null>(null);
	let expandedId = $state<string | null>(null);
	let confirmDeleteId = $state<string | null>(null);
	let busyId = $state('');

	$effect(() => {
		void projectsStore.initialize();
	});

	const projects = $derived(projectsStore.orderedProjects);

	function toggle(projectId: string): void {
		expandedId = expandedId === projectId ? null : projectId;
		confirmDeleteId = null;
	}

	function openEditor(project: Project | null): void {
		editorProject = project;
		editorOpen = true;
	}

	async function startChat(project: Project): Promise<void> {
		busyId = project.id;

		try {
			const { applied } = await startChatInProject(project);

			expandedId = project.id;
			toast.success(`Started a chat in "${project.name}"`, {
				description: applied.length ? applied.join(' · ') : undefined
			});
		} catch (error) {
			toast.error('Could not start the chat', {
				description: error instanceof Error ? error.message : String(error)
			});
		} finally {
			busyId = '';
		}
	}

	async function removeProject(project: Project): Promise<void> {
		await projectsStore.deleteProject(project.id);

		confirmDeleteId = null;

		if (expandedId === project.id) expandedId = null;

		toast.success(`Deleted "${project.name}"`, {
			description: 'Its chats are kept — they now belong to no project.'
		});
	}

	async function moveChat(convId: string, projectId: string | null, name: string): Promise<void> {
		await projectsStore.assign(convId, projectId);
		toast.success(projectId ? `Moved to "${name}"` : 'Removed from its project');
	}
</script>

<div class="flex flex-col gap-1 px-1 pb-2">
	<div class="flex items-center justify-between px-2">
		<h3 class="text-muted-foreground inline-flex h-8 items-center text-xs font-medium">Projects</h3>

		<Button
			variant="ghost"
			size="icon"
			class="h-6 w-6"
			title="New project"
			aria-label="New project"
			onclick={() => openEditor(null)}
		>
			<FolderPlus class="h-3.5 w-3.5" />
		</Button>
	</div>

	{#if projectsStore.error}
		<Badge variant="destructive" class="mx-2 text-[10px]">{projectsStore.error}</Badge>
	{/if}

	{#if projects.length === 0}
		<p class="text-muted-foreground/70 px-2 py-1 text-xs leading-relaxed">
			No projects yet. A project keeps its own chats together with shared instructions, memories
			and settings.
		</p>
	{/if}

	{#each projects as project (project.id)}
		{@const chats = projectsStore.chatsOf(project.id)}
		{@const memories = project.memories.length}
		<Collapsible.Root open={expandedId === project.id}>
			<!-- A plain button, not Collapsible.Trigger: the row also holds a
			     count and needs to keep working while the panel re-renders on
			     every chat rename or new message. -->
			<button
				type="button"
				class="hover:bg-foreground/5 flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left"
				aria-label={`Project ${project.name}`}
				onclick={() => toggle(project.id)}
			>
				<ChevronRight
					class={'h-3.5 w-3.5 shrink-0 transition-transform ' +
						(expandedId === project.id ? 'rotate-90' : '')}
				/>
				<Folder class="h-3.5 w-3.5 shrink-0" />
				<span class="min-w-0 flex-1 truncate text-xs font-medium">{project.name}</span>
				{#if memories > 0}
					<Badge variant="secondary" class="shrink-0 text-[10px]">{memories}m</Badge>
				{/if}
				<Badge variant="secondary" class="shrink-0 text-[10px]">{chats.length}</Badge>
			</button>

			<Collapsible.Content>
				<div class="ml-6 flex flex-col gap-0.5 border-l pl-2">
					{#each chats as chat (chat.id)}
						<div class="hover:bg-foreground/5 group flex items-center gap-1 rounded-md pr-1">
							<button
								type="button"
								class="min-w-0 flex-1 truncate px-2 py-1 text-left text-xs"
								title={chat.name}
								onclick={() => goto(RouterService.chat(chat.id))}
							>
								{chat.name}
							</button>

							<DropdownMenu.Root>
								<DropdownMenu.Trigger
									class="text-muted-foreground/60 hover:text-foreground px-1 text-[10px] opacity-0 group-hover:opacity-100"
									aria-label="Move chat"
								>
									move
								</DropdownMenu.Trigger>
								<DropdownMenu.Content align="start" class="min-w-[10rem]">
									<DropdownMenu.Item onclick={() => moveChat(chat.id, null, '')}>
										No project
									</DropdownMenu.Item>
									{#each projects as target (target.id)}
										{#if target.id !== project.id}
											<DropdownMenu.Item
												onclick={() => moveChat(chat.id, target.id, target.name)}
											>
												{target.name}
											</DropdownMenu.Item>
										{/if}
									{/each}
								</DropdownMenu.Content>
							</DropdownMenu.Root>
						</div>
					{:else}
						<p class="text-muted-foreground/60 px-2 py-1 text-[11px]">No chats yet.</p>
					{/each}

					<div class="mt-1 flex flex-wrap items-center gap-1 px-1 pb-1">
						<Button
							variant="secondary"
							size="sm"
							class="h-6 gap-1 px-2 text-[11px]"
							aria-label="New chat in project"
							disabled={busyId === project.id}
							onclick={() => startChat(project)}
						>
							<Plus class="h-3 w-3" />
							New chat
						</Button>

						<Button
							variant="ghost"
							size="sm"
							class="h-6 gap-1 px-2 text-[11px]"
							aria-label="Edit project"
							onclick={() => openEditor(project)}
						>
							<Pencil class="h-3 w-3" />
							Edit
						</Button>

						{#if confirmDeleteId === project.id}
							<Button
								variant="destructive"
								size="sm"
								class="h-6 gap-1 px-2 text-[11px]"
								onclick={() => removeProject(project)}
							>
								<Trash2 class="h-3 w-3" />
								Delete project (chats kept)
							</Button>
							<Button
								variant="ghost"
								size="sm"
								class="h-6 px-2 text-[11px]"
								onclick={() => (confirmDeleteId = null)}
							>
								Cancel
							</Button>
						{:else}
							<Button
								variant="ghost"
								size="sm"
								class="text-muted-foreground h-6 gap-1 px-2 text-[11px]"
								onclick={() => (confirmDeleteId = project.id)}
							>
								<Trash2 class="h-3 w-3" />
								Delete
							</Button>
						{/if}
					</div>
				</div>
			</Collapsible.Content>
		</Collapsible.Root>
	{/each}
</div>

<ProjectEditor bind:open={editorOpen} project={editorProject} />
