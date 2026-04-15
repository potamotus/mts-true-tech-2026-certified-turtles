<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { onMount, getContext } from 'svelte';
	import { fade } from 'svelte/transition';

	import { showSidebar, mobile, user } from '$lib/stores';
	import { CT_API_BASE_URL } from '$lib/constants';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import SidebarIcon from '$lib/components/icons/Sidebar.svelte';

	const i18n = getContext('i18n');

	const IMAGE_MODELS = [
		{ id: 'qwen-image', name: 'Qwen Image' },
		{ id: 'qwen-image-lightning', name: 'Qwen Image Lightning' }
	];

	let loading = false;

	let prompt = '';
	let selectedModel = IMAGE_MODELS[0].id;
	let sourceImages: string[] = [];
	let generatedImages: { url: string }[] = [];

	let promptTextareaElement: HTMLTextAreaElement;
	let fileInputElement: HTMLInputElement;
	let messagesContainerElement: HTMLDivElement;

	const resizePromptTextarea = () => {
		if (promptTextareaElement) {
			promptTextareaElement.style.height = '';
			promptTextareaElement.style.height =
				Math.min(promptTextareaElement.scrollHeight, 150) + 'px';
		}
	};

	const handleFileUpload = (event: Event) => {
		const input = event.target as HTMLInputElement;
		if (input.files) {
			Array.from(input.files).forEach((file) => {
				const reader = new FileReader();
				reader.onload = (e) => {
					if (e.target?.result) {
						sourceImages = [...sourceImages, e.target.result as string];
					}
				};
				reader.readAsDataURL(file);
			});
		}
	};

	const handleDrop = (event: DragEvent) => {
		event.preventDefault();
		const files = event.dataTransfer?.files;
		if (files) {
			Array.from(files).forEach((file) => {
				if (file.type.startsWith('image/')) {
					const reader = new FileReader();
					reader.onload = (e) => {
						if (e.target?.result) {
							sourceImages = [...sourceImages, e.target.result as string];
						}
					};
					reader.readAsDataURL(file);
				}
			});
		}
	};

	const removeImage = (index: number) => {
		sourceImages = sourceImages.filter((_, i) => i !== index);
	};

	const scrollToBottom = () => {
		if (messagesContainerElement) {
			messagesContainerElement.scrollTop = messagesContainerElement.scrollHeight;
		}
	};

	const submitHandler = async () => {
		if (!prompt.trim()) {
			toast.error($i18n.t('Please enter a prompt'));
			return;
		}

		loading = true;
		try {
			const res = await fetch(`${CT_API_BASE_URL}/v1/images/generations`, {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json',
					...(localStorage.token && { Authorization: `Bearer ${localStorage.token}` })
				},
				body: JSON.stringify({
					model: selectedModel,
					prompt: prompt.trim(),
					n: 1
				})
			});

			if (!res.ok) {
				const err = await res.json().catch(() => ({}));
				throw err.detail || `Error ${res.status}`;
			}

			const data = await res.json();
			if (data?.data) {
				generatedImages = [...generatedImages, ...data.data];
				prompt = '';
				setTimeout(scrollToBottom, 100);
			}
		} catch (error) {
			console.error('Image generation error:', error);
			toast.error(`${error}`);
		} finally {
			loading = false;
		}
	};

	const downloadImage = async (url: string, index: number) => {
		try {
			const response = await fetch(url);
			const blob = await response.blob();
			const blobUrl = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = blobUrl;
			a.download = `image-${Date.now()}-${index}.png`;
			a.click();
			URL.revokeObjectURL(blobUrl);
		} catch (error) {
			toast.error($i18n.t('Failed to download image'));
		}
	};
</script>

{#snippet inputBox()}
	<div
		class="flex flex-col shadow-lg rounded-3xl border border-gray-100/20 dark:border-gray-850/40 px-1 bg-white dark:bg-gray-900 backdrop-blur-sm"
	>
		{#if sourceImages.length > 0}
			<div class="flex flex-wrap gap-2 mx-3 mt-3 pb-1">
				{#each sourceImages as image, index}
					<div class="relative group">
						<img src={image} alt="" class="size-14 rounded-xl object-cover" />
						<div class="absolute -top-1 -right-1">
							<button
								class="bg-white text-black border border-white rounded-full group-hover:visible invisible transition size-5 flex items-center justify-center"
								type="button"
								on:click={() => removeImage(index)}
							>
								<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="size-3.5">
									<path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
								</svg>
							</button>
						</div>
					</div>
				{/each}
			</div>
		{/if}

		<div class="scrollbar-hidden px-3 pb-1 pt-3 max-h-36 overflow-auto">
			<textarea
				bind:this={promptTextareaElement}
				bind:value={prompt}
				class="w-full h-full bg-transparent resize-none outline-hidden text-sm"
				placeholder={sourceImages.length > 0
					? $i18n.t('Describe the edit...')
					: $i18n.t('Describe the image...')}
				on:input={resizePromptTextarea}
				on:focus={resizePromptTextarea}
				on:keydown={(e) => {
					if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !loading) {
						e.preventDefault();
						submitHandler();
					}
				}}
				rows="2"
			/>
		</div>

		<div class="flex justify-between items-center gap-2 px-3 pb-2.5 pt-1">
			<div class="flex items-center gap-1">
				<input type="file" accept="image/*" multiple class="hidden" bind:this={fileInputElement} on:change={handleFileUpload} />
				<button
					type="button"
					class="cursor-pointer p-2 rounded-xl hover:bg-gray-100 dark:hover:bg-gray-850 transition text-gray-500 dark:text-gray-400"
					on:click={() => fileInputElement?.click()}
					on:dragover|preventDefault
					on:drop={handleDrop}
					title={$i18n.t('Add Image')}
				>
					<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="size-5">
						<rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
						<circle cx="8.5" cy="8.5" r="1.5" />
						<polyline points="21 15 16 10 5 21" />
					</svg>
				</button>
			</div>

			<div>
				{#if !loading}
					<button
						disabled={prompt.trim() === ''}
						class="cursor-pointer p-2 rounded-xl bg-black text-white dark:bg-white dark:text-black hover:opacity-80 transition disabled:opacity-30 disabled:cursor-not-allowed"
						on:click={submitHandler}
					>
						<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="size-5">
							<path d="M3.478 2.405a.75.75 0 00-.926.94l2.432 7.905H13.5a.75.75 0 010 1.5H4.984l-2.432 7.905a.75.75 0 00.926.94 60.519 60.519 0 0018.445-8.986.75.75 0 000-1.218A60.517 60.517 0 003.478 2.405z" />
						</svg>
					</button>
				{:else}
					<div class="p-2">
						<Spinner className="size-5" />
					</div>
				{/if}
			</div>
		</div>
	</div>
{/snippet}

<div
	class="h-screen max-h-[100dvh] transition-width duration-200 ease-in-out {$showSidebar
		? 'md:max-w-[calc(100%-var(--sidebar-width))]'
		: ''} w-full max-w-full flex flex-col"
>
	<div in:fade={{ duration: 50 }} class="w-full h-full flex flex-col">
		<!-- Navbar -->
		<nav class="sticky top-0 z-30 w-full">
			<div class="flex items-center w-full pl-1.5 pr-1">
				<div class="flex max-w-full w-full mx-auto px-1.5 md:px-2 pt-0.5 bg-transparent">
					<div class="flex items-center w-full max-w-full">
						{#if $mobile && !$showSidebar}
							<div class="-translate-x-0.5 mr-1 mt-1 self-start flex flex-none items-center text-gray-600 dark:text-gray-400">
								<Tooltip content={$i18n.t('Open Sidebar')}>
									<button
										class="cursor-pointer flex rounded-lg hover:bg-gray-100 dark:hover:bg-gray-850 transition"
										on:click={() => showSidebar.set(true)}
									>
										<div class="self-center p-1.5">
											<SidebarIcon />
										</div>
									</button>
								</Tooltip>
							</div>
						{/if}

						<div class="flex-1 overflow-hidden max-w-full py-0.5 {$showSidebar ? 'ml-1' : ''}">
							<select
								bind:value={selectedModel}
								class="bg-transparent text-lg font-medium text-gray-700 dark:text-gray-200 outline-none cursor-pointer hover:text-black dark:hover:text-white transition py-1"
							>
								{#each IMAGE_MODELS as model}
									<option value={model.id}>{model.name}</option>
								{/each}
							</select>
						</div>
					</div>
				</div>
			</div>
		</nav>

		<!-- Content -->
		<div class="flex flex-col flex-auto z-10 w-full @container overflow-auto">
			{#if generatedImages.length > 0}
				<!-- Images grid -->
				<div
					bind:this={messagesContainerElement}
					class="pb-2.5 flex flex-col justify-between w-full flex-auto overflow-auto h-0 max-w-full z-10 scrollbar-hidden"
				>
					<div class="h-full w-full flex flex-col">
						<div class="w-full max-w-6xl mx-auto px-4 py-4">
							<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
								{#each generatedImages as image, index}
									<button
										class="relative group cursor-pointer"
										on:click={() => downloadImage(image.url, index)}
									>
										<img src={image.url} alt="" class="w-full aspect-square object-cover rounded-2xl border border-gray-100/20 dark:border-gray-800/40" />
										<div class="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition rounded-2xl flex items-center justify-center">
											<svg xmlns="http://www.w3.org/2000/svg" class="w-8 h-8 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
												<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
												<polyline points="7,10 12,15 17,10" />
												<line x1="12" y1="15" x2="12" y2="3" />
											</svg>
										</div>
									</button>
								{/each}
							</div>
						</div>
						<div class="pb-12"></div>
					</div>
				</div>

				<!-- Input at bottom -->
				<div class="pb-2 z-10">
					<div class="w-full font-primary">
						<div class="mx-auto inset-x-0 bg-transparent flex justify-center">
							<div class="flex flex-col px-3 max-w-6xl w-full">
								{@render inputBox()}
							</div>
						</div>
					</div>
				</div>
			{:else}
				<!-- Empty state (like Placeholder) -->
				<div class="flex items-center h-full">
					<div class="m-auto w-full max-w-6xl px-2 @2xl:px-20 translate-y-6 py-24 text-center">
						<div class="w-full text-3xl text-gray-800 dark:text-gray-100 text-center flex items-center gap-4 font-primary">
							<div class="w-full flex flex-col justify-center items-center">
								<div class="flex flex-col items-center w-full px-5 max-w-3xl" in:fade={{ duration: 200 }}>
									<div class="text-3xl font-medium whitespace-nowrap text-gray-700 dark:text-gray-200">
										{$i18n.t('Create an image')}
									</div>
									<div class="text-sm font-normal text-gray-400 dark:text-gray-500 mt-2">
										{$i18n.t('Describe what you want to generate')}
									</div>
								</div>

								<div class="mt-2 mb-2"></div>

								<div class="text-base font-normal @md:max-w-3xl w-full py-3">
									{@render inputBox()}
								</div>
							</div>
						</div>
					</div>
				</div>
			{/if}
		</div>
	</div>
</div>
