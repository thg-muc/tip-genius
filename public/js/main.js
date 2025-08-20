// Main JavaScript for Tip Genius PWA
// Handles UI, data fetching, caching and interactions

// LLM provider configuration with PNG logos
const LLM_PROVIDERS = [
  {
    value: 'Mistral-Large',
    label: 'Mistral Large 2',
    logo: `/images/llm-logos/Mistral.png`,
  },
  {
    value: 'Google-Gemini-Flash',
    label: 'Gemini 2.5 Flash',
    logo: `/images/llm-logos/Google.png`,
  },
  {
    value: 'OpenAI-GPT-Mini',
    label: 'GPT-4o Mini',
    logo: `/images/llm-logos/OpenAI.png`,
  },
  {
    value: 'Deepseek-Chat',
    label: 'DeepSeek V3',
    logo: `/images/llm-logos/DeepSeek.png`,
  },
  {
    value: 'Meta-Llama-70b',
    label: 'Llama 3 70b',
    logo: `/images/llm-logos/Meta.png`,
  },
  {
    value: 'Microsoft-Phi-Medium',
    label: 'Phi 4 Medium',
    logo: `/images/llm-logos/Microsoft.png`,
  },
  {
    value: 'Openrouter-Grok',
    label: 'Grok 3 Mini',
    logo: `/images/llm-logos/xAI.png`,
  },
  // {
  //     value: 'Anthropic-Claude-Haiku',
  //     label: 'Claude Haiku',
  //     logo: `/images/llm-logos/Anthropic.png`
  // },
]

// Default provider is the first one in the list
const DEFAULT_LLM_PROVIDER = LLM_PROVIDERS[0].value

// Default fallback version in YYMMDDhhmm format
let appVersion = '2501010000'

// Configuration will be initialized after version is loaded
let CONFIG

// Global state variables
let currentLeague = localStorage.getItem('lastUsedLeague')
let lastFetchTime = parseInt(localStorage.getItem('lastFetchTime')) || 0
let cachedLeagueData = null
let currentLLM = localStorage.getItem('lastUsedLLM') || DEFAULT_LLM_PROVIDER

// Initialize the application
async function initializeApp() {
  try {
    // Fetch the version information first
    const response = await fetch('/version.json?v=' + Date.now())
    if (response.ok) {
      const data = await response.json()
      appVersion = data.version
      console.log('App initialized with version:', appVersion)
    }
  } catch (error) {
    console.warn('Could not load version, using fallback:', appVersion)
  }

  // Initialize configuration with the correct version
  CONFIG = {
    ALLOWED_ORIGINS: [
      'http://localhost:3000',
      'http://localhost:5000',
      'http://localhost:8000',
      'https://tip-genius.vercel.app',
    ],
    CACHE_DURATION: 3600, // Cache in seconds (1 hour)
    DYNAMIC_CACHE: `tip-genius-dynamic-${appVersion}`,
    DEFAULT_LLM: DEFAULT_LLM_PROVIDER,
    API_URL: '/api/predictions',
  }

  // Try to restore cached data if it's still valid
  const storedData = localStorage.getItem('cachedLeagueData')
  if (storedData && Date.now() - lastFetchTime <= CONFIG.CACHE_DURATION * 1000) {
    try {
      cachedLeagueData = JSON.parse(storedData)
    } catch (e) {
      console.warn('Failed to parse cached data:', e)
      localStorage.removeItem('cachedLeagueData')
      localStorage.removeItem('lastFetchTime')
    }
  }

  // Initialize the application tabs
  await createTabs()

  // Update the active LLM logo in the header
  updateActiveLlmLogo()

  // Set up periodic refresh for data
  setInterval(refreshData, CONFIG.CACHE_DURATION * 1000)
}

// Function to update the active LLM logo in the header
function updateActiveLlmLogo() {
  const activeLlmLogo = document.getElementById('activeLlmLogo')
  if (!activeLlmLogo) return

  // Clear existing content
  activeLlmLogo.innerHTML = ''

  // Find the current LLM provider config
  const provider = LLM_PROVIDERS.find((p) => p.value === currentLLM) || LLM_PROVIDERS[0]

  // Create the image element
  const img = document.createElement('img')
  img.src = provider.logo
  img.alt = `${provider.label} logo`
  img.className = 'w-6 h-6 object-contain'
  img.title = provider.label

  // Add error handling
  img.onerror = () => {
    console.warn(`Failed to load logo for ${provider.label}`)
    // Fallback to text if image fails to load
    const fallback = document.createElement('div')
    fallback.className =
      'w-6 h-6 flex items-center justify-center bg-gray-200 dark:bg-gray-700 rounded-full text-xs font-bold text-gray-800 dark:text-gray-200'
    fallback.textContent = provider.label.charAt(0)
    activeLlmLogo.appendChild(fallback)
  }

  // Add the image to the container
  activeLlmLogo.appendChild(img)
}

// Error handling functions
const showError = (msg) => {
  const err = document.getElementById('error-message')
  err.querySelector('span').textContent = msg
  err.classList.remove('hidden')
}

const hideError = () => document.getElementById('error-message').classList.add('hidden')

// API response parser
const parseApiResponse = (data) =>
  data.result ? JSON.parse(data.result) : Array.isArray(data) ? data : null

// Date utility functions
const formatDate = (dateString) => {
  const [date, time] = dateString.split(' ')
  const [day, month, year] = date.split('.')
  return new Date(`${year}-${month}-${day}T${time}`)
}

const formatDateForDisplay = (date) =>
  date.toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })

const formatTimeForDisplay = (date) =>
  date.toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })

// Check if logo is available
const hasLogo = (logo) => logo && logo !== 'null' && logo !== 'None' && logo !== ''

// UI match element creation
function createMatchElement(match) {
  const matchDate = formatDate(match.commence_time_str)

  // Create the card container with flip functionality
  const container = document.createElement('div')
  container.className = 'card-container cursor-pointer opacity-0'

  // Create the inner container that will flip
  const cardInner = document.createElement('div')
  cardInner.className = 'card-inner'

  // Create the front of the card (using existing design)
  const cardFront = document.createElement('div')
  cardFront.className =
    'card-front bg-white dark:bg-gray-800 bg-opacity-80 dark:bg-opacity-80 backdrop-blur-md shadow-md rounded-2xl py-2 px-2 sm:px-4 transition-all duration-200 hover:shadow-xl'

  // Keep the exact same front content structure
  cardFront.innerHTML = `
   <div class="flex flex-col">
       <div class="flex items-center justify-between mb-1">
           <div class="flex items-center text-xs sm:text-2xl font-semibold text-gray-800 dark:text-gray-200">
               <div class="flex items-center">
                   ${match.home_team}
                   ${
                     hasLogo(match.home_logo)
                       ? `<img src="/images/teams/${match.home_logo}" alt="${match.home_team}" class="w-5 h-5 sm:w-8 sm:h-8 object-contain ml-1 sm:ml-2" loading="lazy">`
                       : '<span class="text-xl sm:text-3xl ml-1 sm:ml-2">⚽️</span>'
                   }
               </div>
               <span class="mx-1 sm:mx-4">–</span>
               <div class="flex items-center">
                   ${
                     hasLogo(match.away_logo)
                       ? `<img src="/images/teams/${match.away_logo}" alt="${match.away_team}" class="w-5 h-5 sm:w-8 sm:h-8 object-contain mr-1 sm:mr-2" loading="lazy">`
                       : '<span class="text-xl sm:text-3xl mr-1 sm:mr-2">⚽️</span>'
                   }
                   ${match.away_team}
               </div>
           </div>
           <div class="font-mono text-sm sm:text-2xl text-gray-500 dark:text-gray-400 ml-2 sm:ml-4">
               ${formatTimeForDisplay(matchDate)}
           </div>
       </div>
       <div class="flex items-end justify-between mt-1">
           <p class="text-xs sm:text-lg text-gray-500 dark:text-gray-400 italic flex-grow pr-2 sm:pr-8">
               "${match.outlook}"
           </p>
           <div class="flex flex-col items-end justify-end ml-3">
               <span class="text-gray-500 dark:text-gray-400 text-[0.55rem] sm:text-base mb-0.5">
                   Prediction
               </span>
               <span class="font-mono font-bold text-xl sm:text-4xl text-sky-700 dark:text-sky-400">
                   ${match.prediction_home}-${match.prediction_away}
               </span>
           </div>
       </div>
   </div>
  `

  // Create the back of the card (with reasoning)
  const cardBack = document.createElement('div')
  cardBack.className =
    'card-back bg-white dark:bg-gray-800 bg-opacity-80 dark:bg-opacity-80 backdrop-blur-md shadow-md rounded-2xl py-2 px-2 sm:px-4 transition-all duration-200'

  // Minimalist design for the back with monospace font and scrollability
  cardBack.innerHTML = `
   <div class="flex flex-col h-full">
       <div class="overflow-y-auto pr-1 -mr-1">
           <p class="font-mono text-[0.65rem] sm:text-base text-gray-700 dark:text-gray-300 py-1">
               ${
                 match.reasoning ||
                 'No detailed reasoning available for this prediction.'
               }
           </p>
       </div>
   </div>
  `

  // Assemble the card structure
  cardInner.appendChild(cardFront)
  cardInner.appendChild(cardBack)
  container.appendChild(cardInner)

  // Add click handler to flip the card
  container.addEventListener('click', () => {
    container.classList.toggle('flipped')
  })

  // Smooth fade-in transition
  requestAnimationFrame(() => {
    container.style.opacity = '1'
    container.style.transition = 'opacity 100ms ease-in-out'
  })

  return container
}

// Match rendering function
function renderMatches(matches, timestamp) {
  const container = document.getElementById('matches-container')
  container.innerHTML = ''

  // First, preload all team logos that will be immediately visible
  const firstMatchLogos = []
  if (matches.length > 0) {
    if (hasLogo(matches[0].home_logo))
      firstMatchLogos.push(`/images/teams/${matches[0].home_logo}`)
    if (hasLogo(matches[0].away_logo))
      firstMatchLogos.push(`/images/teams/${matches[0].away_logo}`)
  }

  // Helper to show first elements once preloading is complete
  const showFirstElements = () => {
    // Show the first 2 elements (typically date header + first match)
    const elementsToShow = Math.min(2, elements.length)
    for (let i = 0; i < elementsToShow; i++) {
      elements[i].style.transition = 'none'
      elements[i].style.opacity = '1'
    }

    // Staggered fade-in for remaining elements with consistent timing
    for (let i = elementsToShow; i < elements.length; i++) {
      const element = elements[i]
      void element.offsetWidth
      element.style.transition = 'opacity 300ms ease-in-out'
      setTimeout(
        () => {
          element.style.opacity = '1'
        },
        80 * (i - elementsToShow)
      )
    }
  }

  // If first match has logos, preload them before rendering
  let preloadPromise = Promise.resolve()
  if (firstMatchLogos.length > 0) {
    preloadPromise = Promise.all(
      firstMatchLogos.map((url) => {
        return new Promise((resolve) => {
          const img = new Image()
          img.onload = resolve
          img.onerror = resolve
          img.src = url
        })
      })
    )
  }

  // Track elements for staggered fade-in
  let currentDate = null
  const elements = []

  // Process date headers and matches
  matches.forEach((match) => {
    const matchDate = formatDate(match.commence_time_str)
    const formattedDate = formatDateForDisplay(matchDate)

    // Create date header if needed
    if (formattedDate !== currentDate) {
      const dateHeader = document.createElement('h2')
      dateHeader.className =
        'text-base sm:text-2xl font-bold tracking-wide text-gray-500 dark:text-gray-400 mb-0 mt-8'
      dateHeader.textContent = formattedDate
      dateHeader.style.opacity = '0'
      container.appendChild(dateHeader)
      elements.push(dateHeader)
      currentDate = formattedDate
    }

    // Create match element (hidden initially)
    const matchElement = document.createElement('div')
    matchElement.className = 'card-container cursor-pointer'
    matchElement.style.opacity = '0'

    // Add the inner HTML for the match card
    matchElement.innerHTML = `
      <div class="card-inner">
        <div class="card-front bg-white dark:bg-gray-800 bg-opacity-80 dark:bg-opacity-80 backdrop-blur-md shadow-md rounded-2xl py-2 px-2 sm:px-4 transition-all duration-200 hover:shadow-xl">
          <div class="flex flex-col">
            <div class="flex items-center justify-between mb-1">
              <div class="flex items-center text-xs sm:text-2xl font-semibold text-gray-800 dark:text-gray-200">
                <div class="flex items-center">
                  ${match.home_team}
                  ${
                    hasLogo(match.home_logo)
                      ? `<img src="/images/teams/${match.home_logo}" alt="${match.home_team}" class="w-5 h-5 sm:w-8 sm:h-8 object-contain ml-1 sm:ml-2" loading="lazy">`
                      : '<span class="text-xl sm:text-3xl ml-1 sm:ml-2">⚽️</span>'
                  }
                </div>
                <span class="mx-1 sm:mx-4">–</span>
                <div class="flex items-center">
                  ${
                    hasLogo(match.away_logo)
                      ? `<img src="/images/teams/${match.away_logo}" alt="${match.away_team}" class="w-5 h-5 sm:w-8 sm:h-8 object-contain mr-1 sm:mr-2" loading="lazy">`
                      : '<span class="text-xl sm:text-3xl mr-1 sm:mr-2">⚽️</span>'
                  }
                  ${match.away_team}
                </div>
              </div>
              <div class="font-mono text-sm sm:text-2xl text-gray-500 dark:text-gray-400 ml-2 sm:ml-4">
                ${formatTimeForDisplay(matchDate)}
              </div>
            </div>
            <div class="flex items-end justify-between mt-1">
              <p class="text-xs sm:text-lg text-gray-500 dark:text-gray-400 italic flex-grow pr-2 sm:pr-8">
                "${match.outlook}"
              </p>
              <div class="flex flex-col items-end justify-end ml-3">
                <span class="text-gray-500 dark:text-gray-400 text-[0.55rem] sm:text-base mb-0.5">
                  Prediction
                </span>
                <span class="font-mono font-bold text-xl sm:text-4xl text-sky-700 dark:text-sky-400">
                  ${match.prediction_home}-${match.prediction_away}
                </span>
              </div>
            </div>
          </div>
        </div>
        <div class="card-back bg-white dark:bg-gray-800 bg-opacity-80 dark:bg-opacity-80 backdrop-blur-md shadow-md rounded-2xl py-2 px-2 sm:px-4 transition-all duration-200">
          <div class="flex flex-col h-full">
            <div class="overflow-y-auto pr-1 -mr-1">
              <p class="font-mono text-[0.65rem] sm:text-base text-gray-700 dark:text-gray-300 py-1">
                ${
                  match.reasoning ||
                  'No detailed reasoning available for this prediction.'
                }
              </p>
            </div>
          </div>
        </div>
      </div>
    `

    // Add click handler to flip the card
    matchElement.addEventListener('click', () => {
      matchElement.classList.toggle('flipped')
    })

    container.appendChild(matchElement)
    elements.push(matchElement)
  })

  // Add timestamp and version info
  if (timestamp) {
    const timestampElement = document.createElement('p')
    timestampElement.className =
      'text-[0.65rem] sm:text-xs text-gray-500 dark:text-gray-500 italic text-center mt-6 mb-2'
    timestampElement.textContent = `Last Update: ${timestamp}`
    timestampElement.style.opacity = '0'
    container.appendChild(timestampElement)
    elements.push(timestampElement)

    const versionElement = document.createElement('p')
    versionElement.className =
      'text-[0.65rem] sm:text-xs text-gray-500 dark:text-gray-500 italic text-center mt-2 mb-6'
    versionElement.textContent = `Build Version: ${appVersion}`
    versionElement.style.opacity = '0'
    container.appendChild(versionElement)
    elements.push(versionElement)
  }

  // Once preloading is complete, show elements with staggering
  preloadPromise.then(showFirstElements)
}

function preloadTeamLogos(leagues) {
  if (!leagues || leagues.length === 0) return

  // Get current league (first one or from localStorage)
  const currentLeague = localStorage.getItem('lastUsedLeague') || leagues[0].name
  const currentLeagueData = leagues.find((l) => l.name === currentLeague)

  // Step 1: Create a prioritized logo queue system
  const priorityLogos = new Set() // Current league logos (high priority)
  const backgroundLogos = new Set() // Other leagues' logos (low priority)

  // Categorize logos into priority groups
  leagues.forEach((league) => {
    league.matches.forEach((match) => {
      const logoSet = league.name === currentLeague ? priorityLogos : backgroundLogos

      if (hasLogo(match.home_logo)) logoSet.add(match.home_logo)
      if (hasLogo(match.away_logo)) logoSet.add(match.away_logo)
    })
  })

  // Step 2: Preload current league logos immediately
  preloadLogoSet(priorityLogos, true)

  // Step 3: Load other leagues' logos only during browser idle time
  if (backgroundLogos.size > 0) {
    scheduleBackgroundLoading(Array.from(backgroundLogos))
  }

  console.log(
    `Prioritized preloading: ${priorityLogos.size} immediate, ${backgroundLogos.size} deferred`
  )
}

// Helper function to preload a set of logos
function preloadLogoSet(logoSet, isHighPriority = false) {
  // Convert to array for batch processing if needed
  const logos = Array.from(logoSet)

  // Simple check to avoid reloading already cached images
  const cachedLogos = new Set(
    Object.keys(
      window.performance
        .getEntriesByType('resource')
        .filter((r) => r.name.includes('/images/teams/'))
        .map((r) => r.name.split('/').pop())
    )
  )

  // Process logos
  logos.forEach((logoName) => {
    // Skip if we have evidence it's already cached
    if (cachedLogos.has(logoName)) return

    const img = new Image()

    // Set loading priority based on group
    if (!isHighPriority) {
      img.loading = 'lazy' // Use native lazy loading for low priority
      img.fetchPriority = 'low'
    }

    // Load the image
    img.src = `/images/teams/${logoName}`
  })
}

// Helper function to schedule background loading in batches
function scheduleBackgroundLoading(logos) {
  // Use requestIdleCallback if available, otherwise use a deferred timeout
  const scheduleWhenIdle = window.requestIdleCallback || ((fn) => setTimeout(fn, 1000)) // Fallback with 1 second delay

  // Process in smaller batches to avoid locking up the browser
  const BATCH_SIZE = 10
  let currentIndex = 0

  function loadNextBatch(deadline) {
    // Check if we're done
    if (currentIndex >= logos.length) return

    // Calculate how many to process in this batch
    const endIndex = Math.min(currentIndex + BATCH_SIZE, logos.length)
    const timeRemaining =
      deadline && typeof deadline.timeRemaining === 'function'
        ? deadline.timeRemaining()
        : 50 // Default time slice of 50ms

    // Skip this cycle if we're low on time
    if (timeRemaining < 10) {
      scheduleWhenIdle(loadNextBatch)
      return
    }

    // Process a batch
    const batch = logos.slice(currentIndex, endIndex)
    preloadLogoSet(new Set(batch), false)
    currentIndex = endIndex

    // Schedule next batch
    if (currentIndex < logos.length) {
      scheduleWhenIdle(loadNextBatch)
    }
  }

  // Start the process
  scheduleWhenIdle(loadNextBatch)
}

// League Data processing and rendering
async function loadLeagueData(leagueName) {
  console.log('Loading data for league:', leagueName)
  hideError()
  const container = document.getElementById('matches-container')
  container.style.opacity = '0'

  try {
    const leagues = cachedLeagueData || (await fetchLeagueData())
    const leagueData = leagues.find((l) => l.name === leagueName)

    if (leagueData) {
      const sortedMatches = [...leagueData.matches].sort(
        (a, b) => formatDate(a.commence_time_str) - formatDate(b.commence_time_str)
      )

      renderMatches(sortedMatches, leagueData.timestamp)

      // Smooth fade-in transition
      requestAnimationFrame(() => {
        container.style.opacity = '1'
        container.style.transition = 'opacity 100ms ease-in-out'
      })

      localStorage.setItem('lastUsedLeague', leagueName)
    } else {
      showError(`Currently no predictions available for this LLM / League.`)
    }
  } catch (error) {
    console.error(`Error loading ${leagueName} data:`, error)
    showError('Failed to load match predictions. Please try again later.')
  }
}

// API data fetching (with caching)
async function fetchLeagueData() {
  console.log('Fetching data using LLM Provider:', currentLLM)
  const currentTime = Date.now()
  const timeDiff = currentTime - lastFetchTime
  const cacheDuration = CONFIG.CACHE_DURATION * 1000

  // Check if we have valid cached data
  if (cachedLeagueData !== null && timeDiff <= cacheDuration) {
    console.log('Using cached data')
    return cachedLeagueData
  }

  // If we reach here, we need to fetch fresh data
  console.log('Cache invalidated or empty, fetching fresh data')
  cachedLeagueData = null

  // Clear KV cache if using service worker
  if ('caches' in window) {
    try {
      const cache = await caches.open(CONFIG.DYNAMIC_CACHE)
      await cache.delete(CONFIG.API_URL)
    } catch (error) {
      console.warn('Cache clear failed:', error)
    }
  }

  // Fetch fresh data
  const storeKey = `Match_Predictions_${currentLLM}_FourPointsScoring_named_with-info`
  const response = await fetch(`${CONFIG.API_URL}?key=${storeKey}`)

  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`)

  const data = parseApiResponse(await response.json())
  if (data) {
    cachedLeagueData = data
    lastFetchTime = currentTime
    // Store the last fetch time in local storage
    localStorage.setItem('lastFetchTime', currentTime.toString())
    // Store the fetched data in local storage
    localStorage.setItem('cachedLeagueData', JSON.stringify(data))

    // Preload the Team Data
    preloadTeamLogos(data)
  }
  return data
}

// Tab creation and handling
function setActiveTab(tabId) {
  document.querySelectorAll('button[id^="tab-"]').forEach((tab) => {
    const isActive = tab.id === tabId

    // First, remove all state-related classes
    tab.classList.remove(
      // Inactive state classes
      'bg-gray-100',
      'dark:bg-slate-900',
      'text-gray-500',
      'dark:text-gray-400',
      'font-normal',
      'hover:bg-gray-50',
      'dark:hover:bg-gray-700',
      'hover:text-sky-700',
      'dark:hover:text-sky-400',
      'border-transparent',
      // Active state classes
      'bg-white',
      'dark:bg-gray-800',
      'text-sky-700',
      'dark:text-sky-400',
      'font-semibold',
      'border-sky-500',
      'dark:border-sky-400',
      'shadow-md',
      'tab-active'
    )

    // Then add appropriate classes for current state
    if (isActive) {
      tab.classList.add(
        'bg-white',
        'dark:bg-gray-800',
        'text-sky-700',
        'dark:text-sky-400',
        'font-semibold',
        'border-sky-500',
        'dark:border-sky-400',
        'shadow-md',
        'tab-active'
      )
    } else {
      tab.classList.add(
        'bg-gray-100',
        'dark:bg-slate-900',
        'text-gray-500',
        'dark:text-gray-400',
        'font-normal',
        'hover:bg-gray-50',
        'dark:hover:bg-gray-700',
        'hover:text-sky-700',
        'dark:hover:text-sky-400',
        'border-transparent'
      )
    }
  })
}

// Create Tab helper function
function createTab(league, index, totalLeagues) {
  const button = document.createElement('button')
  const tabId = `tab-${league.name.replace(/\s+/g, '-').toLowerCase()}`
  button.id = tabId

  // Base classes for tabs
  const baseClasses = [
    'flex',
    'items-center',
    'justify-center',
    'gap-2',
    'px-2',
    'sm:px-4',
    'py-3',
    'sm:py-4',
    'text-[10px]',
    'sm:text-lg',
    'focus:z-10',
    'flex-1',
    'sm:flex-none',
    'sm:w-56',
    'transition-all',
    'duration-300',
    'relative',
    'border-b-2',
    'border-transparent',
    'tracking-wide',
  ]

  // Conditional rounding classes
  if (index === 0) baseClasses.push('rounded-l-md')
  if (index === totalLeagues - 1) baseClasses.push('rounded-r-md')

  // Interactive state classes
  const stateClasses = [
    'bg-gray-100',
    'dark:bg-slate-900',
    'text-gray-500',
    'dark:text-gray-400',
    'font-normal',
    'hover:bg-gray-50',
    'dark:hover:bg-gray-700',
    'hover:text-sky-700',
    'dark:hover:text-sky-400',
    'hover:shadow-md',
    'hover:-translate-y-0.5',
  ]

  button.className = [...baseClasses, ...stateClasses].join(' ')
  return button
}

// Create Tabs function
async function createTabs() {
  const tabContainer = document.getElementById('league-tabs')

  try {
    const leagues = cachedLeagueData || (await fetchLeagueData())

    // Clear existing tabs
    tabContainer.innerHTML = ''

    leagues.forEach((league, index) => {
      const button = createTab(league, index, leagues.length)

      // Create and add the logo image
      const img = document.createElement('img')
      const logoFilename = league.name
        .split(' - ')
        .map((part) => part.replace(/\s+/g, ''))
        .join('-')
      img.src = `/images/leagues/${logoFilename}.png`
      img.alt = `${league.name} logo`
      img.className = 'w-4 h-4 sm:w-6 sm:h-6 object-contain'
      img.loading = 'lazy'
      img.onerror = () => {
        console.warn(
          'Failed to load logo for: %s (tried path: %s)',
          league.name,
          img.src
        )
        img.style.display = 'none'
      }

      // Create span for league name
      const span = document.createElement('span')
      let displayName = league.name
        .replace(/UEFA\s+/, '') // Remove "UEFA" prefix
        .split(' - ')[0] // Remove country suffix
      span.textContent = displayName

      // Add both elements to button
      button.appendChild(img)
      button.appendChild(span)

      // Add click handler
      button.addEventListener('click', () => {
        hideError()
        currentLeague = league.name
        loadLeagueData(currentLeague)
        setActiveTab(button.id)
      })

      tabContainer.appendChild(button)
    })

    // Initialize with default or stored league
    if (!currentLeague || !leagues.some((l) => l.name === currentLeague)) {
      currentLeague = leagues[0].name
    }

    // Make sure to set active tab before loading data
    const activeTabId = `tab-${currentLeague.replace(/\s+/g, '-').toLowerCase()}`
    setActiveTab(activeTabId)
    loadLeagueData(currentLeague)
  } catch (error) {
    console.error('Error loading league data:', error)
    showError('Failed to load league data. Please try again later.')
  }
}

// Menu handling
document.addEventListener('DOMContentLoaded', () => {
  // Initialize the application
  initializeApp()

  const menuButton = document.getElementById('menuButton')
  const dropdownMenu = document.getElementById('dropdownMenu')

  // Initialize currentLLM if it hasn't been done yet
  if (typeof currentLLM === 'undefined') {
    currentLLM = localStorage.getItem('lastUsedLLM') || CONFIG.DEFAULT_LLM
  }

  // Make sure the container exists
  const providersContainer = document.getElementById('llm-providers')
  if (!providersContainer) {
    console.warn('LLM providers container not found')
    return
  }

  // Clear any existing content
  providersContainer.innerHTML = ''

  // Generate radio buttons for LLM providers
  LLM_PROVIDERS.forEach((provider) => {
    const label = document.createElement('label')
    label.className =
      'flex items-center space-x-3 cursor-pointer hover:bg-gray-200 dark:hover:bg-gray-800 p-2 rounded-lg transition-colors'

    const input = document.createElement('input')
    input.type = 'radio'
    input.name = 'llm-provider'
    input.value = provider.value
    input.className = 'form-radio text-sky-700 dark:text-sky-400'
    input.checked = provider.value === currentLLM

    // Create container for logo and label
    const contentContainer = document.createElement('div')
    contentContainer.className = 'flex items-center ml-2'

    // Add the logo image
    const img = document.createElement('img')
    img.src = provider.logo
    img.alt = `${provider.label} logo`
    img.className = 'w-5 h-5 mr-2 object-contain' // Scale image while maintaining aspect ratio

    // Add error handling for the image
    img.onerror = () => {
      console.warn(`Failed to load logo for ${provider.label}`)
      img.style.display = 'none'
    }

    // Add the label text
    const span = document.createElement('span')
    span.className =
      'text-sm sm:text-base text-gray-700 dark:text-gray-300 sm:whitespace-nowrap'
    span.textContent = provider.label

    // Assemble all pieces
    contentContainer.appendChild(img)
    contentContainer.appendChild(span)

    label.appendChild(input)
    label.appendChild(contentContainer)
    providersContainer.appendChild(label)
  })

  // Get all radio buttons after they've been created
  const llmRadios = document.querySelectorAll('input[name="llm-provider"]')

  menuButton.addEventListener('click', () => {
    dropdownMenu.classList.toggle('hidden')
  })

  document.addEventListener('click', (event) => {
    if (!menuButton.contains(event.target) && !dropdownMenu.contains(event.target)) {
      dropdownMenu.classList.add('hidden')
    }
  })

  llmRadios.forEach((radio) => {
    radio.addEventListener('change', async (event) => {
      currentLLM = event.target.value
      localStorage.setItem('lastUsedLLM', currentLLM)

      // Update the active LLM logo in the header
      updateActiveLlmLogo()

      cachedLeagueData = null
      lastFetchTime = 0
      if (currentLeague) {
        await loadLeagueData(currentLeague)
      }
      dropdownMenu.classList.add('hidden')
    })
  })

  document
    .querySelector('header a[href="#about"]')
    .addEventListener('click', scrollToAbout)
  // Add loaded class to body after all initialization is complete
  document.body.classList.add('loaded')
  // Verify initialization
  console.log('Menu initialization complete, providers:', LLM_PROVIDERS.length)
})

// Navigation functions
const scrollToTop = () => {
  window.scrollTo({ top: 0, behavior: 'smooth' })
  return false
}

const scrollToAbout = (event) => {
  event.preventDefault()
  document.getElementById('about').scrollIntoView({ behavior: 'smooth' })
}

// Set up periodic refresh only if we don't have valid cached data
const refreshData = () => {
  const currentTime = Date.now()
  const lastFetch = parseInt(localStorage.getItem('lastFetchTime')) || 0

  if (currentTime - lastFetch > CONFIG.CACHE_DURATION * 1000) {
    if (currentLeague) {
      loadLeagueData(currentLeague)
    }
  }
}

// Service Worker registration with update notification
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker
      .register('/sw.js')
      .then((registration) => {
        console.log('ServiceWorker registration successful')

        // Check for updates to the Service Worker
        registration.addEventListener('updatefound', () => {
          const newWorker = registration.installing

          newWorker.addEventListener('statechange', () => {
            // When the service worker is activated
            if (newWorker.state === 'activated' && navigator.serviceWorker.controller) {
              // Show update notification if not first install
              const notification = document.createElement('div')
              notification.className =
                'fixed bottom-4 right-4 bg-white dark:bg-gray-800 rounded-lg shadow-lg p-4 z-50 flex items-center'
              notification.innerHTML = `
                <div class="mr-3 text-sky-700 dark:text-sky-400">
                  <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                </div>
                <div class="flex-1">
                  <p class="text-sm text-gray-800 dark:text-gray-200">App updated!</p>
                  <p class="text-xs text-gray-500 dark:text-gray-400">Refresh to apply changes</p>
                </div>
                <button id="update-btn" class="ml-4 px-3 py-1 bg-sky-700 text-white text-sm rounded hover:bg-sky-800 transition-colors">
                  Refresh
                </button>
              `

              document.body.appendChild(notification)
              document.getElementById('update-btn').addEventListener('click', () => {
                window.location.reload()
              })
            }
          })
        })
      })
      .catch((err) => {
        console.error('ServiceWorker registration failed:', err)
        // Continue without service worker functionality
      })
  })

  // Handle page refresh when service worker is updated
  let refreshing = false
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!refreshing) {
      refreshing = true
      window.location.reload() // Refresh the page to get the new version
    }
  })
}

// Analytics (only in production)
if (
  window.location.hostname !== 'localhost' &&
  window.location.hostname !== '127.0.0.1'
) {
  // Vercel Analytics
  window.va =
    window.va ||
    function () {
      ;(window.vaq = window.vaq || []).push(arguments)
    }
  const vaScript = document.createElement('script')
  vaScript.defer = true
  vaScript.src = '/_vercel/insights/script.js'
  document.head.appendChild(vaScript)

  // Vercel Speed Insights
  window.si =
    window.si ||
    function () {
      ;(window.siq = window.siq || []).push(arguments)
    }
  const siScript = document.createElement('script')
  siScript.defer = true
  siScript.src = '/_vercel/speed-insights/script.js'
  document.head.appendChild(siScript)
}
