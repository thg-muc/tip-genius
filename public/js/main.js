// Config and utility functions for the Tip Genius web app
const CONFIG = {
    ALLOWED_ORIGINS: [
        'http://localhost:3000',
        'http://localhost:5000',
        'http://localhost:8000',
        'https://tip-genius.vercel.app'
    ],
    CACHE_DURATION: 3600, // Cache in seconds
    DYNAMIC_CACHE: 'tip-genius-dynamic-2.0.0',
    DEFAULT_LLM: 'Mistral-Large',
    API_URL: '/api/predictions'
};

// Global variables
let currentLeague = localStorage.getItem('lastUsedLeague');
let lastFetchTime = parseInt(localStorage.getItem('lastFetchTime')) || 0;
let cachedLeagueData = null;
let currentLLM = localStorage.getItem('lastUsedLLM') || CONFIG.DEFAULT_LLM;

// Try to restore cached data if it's still valid
const storedData = localStorage.getItem('cachedLeagueData');
if (storedData && Date.now() - lastFetchTime <= CONFIG.CACHE_DURATION * 1000) {
    try {
        cachedLeagueData = JSON.parse(storedData);
    } catch (e) {
        console.warn('Failed to parse cached data:', e);
        localStorage.removeItem('cachedLeagueData');
        localStorage.removeItem('lastFetchTime');
    }
}

// Error handling functions
const showError = msg => {
    const err = document.getElementById('error-message');
    err.querySelector('span').textContent = msg;
    err.classList.remove('hidden');
};

const hideError = () => document.getElementById('error-message').classList.add('hidden');

// API response parser
const parseApiResponse = data => data.result ? JSON.parse(data.result) : (Array.isArray(data) ? data : null);

// Date utility functions
const formatDate = dateString => {
    const [date, time] = dateString.split(' ');
    const [day, month, year] = date.split('.');
    return new Date(`${year}-${month}-${day}T${time}`);
};

const formatDateForDisplay = date => date.toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric'
});

const formatTimeForDisplay = date => date.toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false
});

// UI element creation functions
function createMatchElement(match) {
    const matchDate = formatDate(match.commence_time_str);
    const element = document.createElement('div');
    element.className = 'bg-white dark:bg-dark-card bg-opacity-80 dark:bg-opacity-80 backdrop-blur-md shadow-md rounded-2xl py-2 px-3 sm:px-4 transition-all duration-200 hover:shadow-xl opacity-0';

    element.innerHTML = `
        <div class="flex flex-col">
            <div class="flex items-center justify-between mb-1">
                <div class="text-sm sm:text-2xl font-semibold text-gray-800 dark:text-gray-200">
                    ${match.home_team} vs ${match.away_team}
                </div>
                <div class="font-mono text-sm sm:text-2xl text-gray-500 dark:text-gray-400 ml-4">
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
    `;
    
    // Fade in animation
    requestAnimationFrame(() => {
        element.style.opacity = '1';
        element.style.transition = 'opacity 100ms ease-in-out';
    });
    
    return element;
}

// Match rendering function
function renderMatches(matches, timestamp) {
    const container = document.getElementById('matches-container');
    container.innerHTML = '';
    
    let currentDate = null;
    
    matches.forEach(match => {
        const matchDate = formatDate(match.commence_time_str);
        const formattedDate = formatDateForDisplay(matchDate);
        
        // Create date header if needed
        if (formattedDate !== currentDate) {
            const dateHeader = document.createElement('h2');
            dateHeader.className = 'text-base sm:text-2xl font-bold text-gray-500 dark:text-gray-400 mb-0 mt-8';
            dateHeader.textContent = formattedDate;
            container.appendChild(dateHeader);
            currentDate = formattedDate;
        }
        
        container.appendChild(createMatchElement(match));
    });
    
    // Add timestamp if available
    if (timestamp) {
        const timestampElement = document.createElement('p');
        timestampElement.className = 'text-xs sm:text-sm text-gray-500 dark:text-gray-400 italic text-center mt-8';
        timestampElement.textContent = `Last Update: ${timestamp}`;
        container.appendChild(timestampElement);
    }
}

// League Data processing and rendering
async function loadLeagueData(leagueName) {
    console.log('Loading data for league:', leagueName);
    hideError();
    const container = document.getElementById('matches-container');
    container.style.opacity = '0';
    
    try {
        const leagues = cachedLeagueData || await fetchLeagueData();
        const leagueData = leagues.find(l => l.name === leagueName);
        
        if (leagueData) {
            const sortedMatches = [...leagueData.matches].sort(
                (a, b) => formatDate(a.commence_time_str) - formatDate(b.commence_time_str)
            );
            
            renderMatches(sortedMatches, leagueData.timestamp);
            
            // Smooth fade-in transition
            requestAnimationFrame(() => {
                container.style.opacity = '1';
                container.style.transition = 'opacity 100ms ease-in-out';
            });
            
            localStorage.setItem('lastUsedLeague', leagueName);
        } else {
            showError(`No data found for league: ${leagueName}`);
        }
    } catch (error) {
        console.error(`Error loading ${leagueName} data:`, error);
        showError('Failed to load match predictions. Please try again later.');
    }
}

// API data fetching (with caching)
async function fetchLeagueData() {
    console.log('Fetching data using LLM Provider:', currentLLM);
    const currentTime = Date.now();
    const timeDiff = currentTime - lastFetchTime;
    const cacheDuration = CONFIG.CACHE_DURATION * 1000;
    
    // Check if we have valid cached data
    if (cachedLeagueData !== null && timeDiff <= cacheDuration) {
        console.log('Using cached data');
        return cachedLeagueData;
    }
    
    // If we reach here, we need to fetch fresh data
    console.log('Cache invalidated or empty, fetching fresh data');
    cachedLeagueData = null;
    
    // Clear KV cache if using service worker
    if ('caches' in window) {
        try {
            const cache = await caches.open(CONFIG.DYNAMIC_CACHE);
            await cache.delete(CONFIG.API_URL);
        } catch (error) {
            console.warn('Cache clear failed:', error);
        }
    }
    
    // Fetch fresh data
    const storeKey = `Match_Predictions_${currentLLM}_FourPointsScoring_named_with-info`;
    const response = await fetch(`${CONFIG.API_URL}?key=${storeKey}`);
    
    if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
    
    const data = parseApiResponse(await response.json());
    if (data) {
        cachedLeagueData = data;
        lastFetchTime = currentTime;
        localStorage.setItem('lastFetchTime', currentTime.toString());
        // Also cache the data itself
        localStorage.setItem('cachedLeagueData', JSON.stringify(data));
    }
    return data;
}

// Tab management functions
function setActiveTab(tabId) {
    document.querySelectorAll('button[id^="tab-"]').forEach(tab => {
        const isActive = tab.id === tabId;
        if (isActive) {
            tab.classList.remove('bg-gray-100', 'dark:bg-dark', 'text-gray-500', 'dark:text-gray-400', 'font-normal');
            tab.classList.add('bg-white', 'dark:bg-dark-card', 'text-sky-700', 'dark:text-sky-400', 'font-semibold');
        } else {
            tab.classList.remove('bg-white', 'dark:bg-dark-card', 'text-sky-700', 'dark:text-sky-400', 'font-semibold');
            tab.classList.add('bg-gray-100', 'dark:bg-dark', 'text-gray-500', 'dark:text-gray-400', 'font-normal');
        }
    });
}

// Tab creation and initialization
async function createTabs() {
    const tabContainer = document.getElementById('league-tabs');
    
    try {
        const leagues = cachedLeagueData || await fetchLeagueData();
        
        // Clear existing tabs
        tabContainer.innerHTML = '';
        
        leagues.forEach((league, index) => {
            const button = document.createElement('button');
            const tabId = `tab-${league.name.replace(/\s+/g, '-').toLowerCase()}`;
            
            button.id = tabId;
            button.className = `
                px-4 py-2 text-xs sm:text-lg focus:z-10 
                transition duration-200 ease-in-out shadow-md
                bg-gray-100 dark:bg-dark text-gray-500 dark:text-gray-400 font-normal
                hover:bg-gray-50 dark:hover:bg-dark hover:text-sky-700 dark:hover:text-sky-400
                ${index === 0 ? 'rounded-l-lg' : ''}
                ${index === leagues.length - 1 ? 'rounded-r-lg' : ''}
            `;
            button.textContent = league.name;
            
            button.addEventListener('click', () => {
                hideError();
                currentLeague = league.name;
                loadLeagueData(currentLeague);
                setActiveTab(tabId);
            });
            
            tabContainer.appendChild(button);
        });
        
        // Initialize with default or stored league
        if (!currentLeague || !leagues.some(l => l.name === currentLeague)) {
            currentLeague = leagues[0].name;
        }
        
        // Make sure to set active tab before loading data
        const activeTabId = `tab-${currentLeague.replace(/\s+/g, '-').toLowerCase()}`;
        setActiveTab(activeTabId);
        loadLeagueData(currentLeague);
        
    } catch (error) {
        console.error('Error loading league data:', error);
        showError('Failed to load league data. Please try again later.');
    }
}

// Menu handling
document.addEventListener('DOMContentLoaded', () => {
    const menuButton = document.getElementById('menuButton');
    const dropdownMenu = document.getElementById('dropdownMenu');
    
    // Initialize currentLLM if it hasn't been done yet
    if (typeof currentLLM === 'undefined') {
        currentLLM = localStorage.getItem('lastUsedLLM') || CONFIG.DEFAULT_LLM;
    }

    // LLM provider configuration with PNG logos
    const llmProviders = [
        { 
            value: 'Mistral-Large', 
            label: 'Mistral Large',
            logo: `/images/llm-logos/Mistral.png`
        },
        { 
            value: 'Google-Gemini-Flash', 
            label: 'Gemini Flash',
            logo: `/images/llm-logos/Google.png`
        },
        { 
            value: 'Deepseek-Chat', 
            label: 'DeepSeek',
            logo: `/images/llm-logos/DeepSeek.png`
        },
        { 
            value: 'Meta-Llama-70b', 
            label: 'Llama 70b',
            logo: `/images/llm-logos/Meta.png`
        },
        { 
            value: 'Microsoft-Phi-Medium', 
            label: 'Phi Medium',
            logo: `/images/llm-logos/Microsoft.png`
        },
        // { 
        //     value: 'Anthropic-Claude-Haiku', 
        //     label: 'Claude Haiku',
        //     logo: `/images/llm-logos/Anthropic.png`
        // },
        // { 
        //     value: 'OpenAI-ChatGPT-Mini', 
        //     label: 'ChatGPT Mini',
        //     logo: `/images/llm-logos/OpenAI.png`
        // },
    ];

    // Make sure the container exists
    const providersContainer = document.getElementById('llm-providers');
    if (!providersContainer) {
        console.warn('LLM providers container not found');
        return;
    }

    // Clear any existing content
    providersContainer.innerHTML = '';

    // Generate radio buttons for LLM providers
    llmProviders.forEach(provider => {
        const label = document.createElement('label');
        label.className = 'flex items-center space-x-3 cursor-pointer hover:bg-gray-50 dark:hover:bg-dark-card p-2 rounded-lg transition-colors';
        
        const input = document.createElement('input');
        input.type = 'radio';
        input.name = 'llm-provider';
        input.value = provider.value;
        input.className = 'form-radio text-sky-700 dark:text-sky-400';
        input.checked = provider.value === currentLLM;
        
        // Create container for logo and label
        const contentContainer = document.createElement('div');
        contentContainer.className = 'flex items-center ml-2';
        
        // Add the logo image
        const img = document.createElement('img');
        img.src = provider.logo;
        img.alt = `${provider.label} logo`;
        img.className = 'w-5 h-5 mr-2 object-contain'; // Scale image while maintaining aspect ratio
        
        // Add error handling for the image
        img.onerror = () => {
            console.warn(`Failed to load logo for ${provider.label}`);
            img.style.display = 'none';
        };
        
        // Add the label text
        const span = document.createElement('span');
        span.className = 'text-base text-gray-700 dark:text-gray-300';
        span.textContent = provider.label;
        
        // Assemble all pieces
        contentContainer.appendChild(img);
        contentContainer.appendChild(span);
        
        label.appendChild(input);
        label.appendChild(contentContainer);
        providersContainer.appendChild(label);
    });

    // Get all radio buttons after they've been created
    const llmRadios = document.querySelectorAll('input[name="llm-provider"]');

    menuButton.addEventListener('click', () => {
        dropdownMenu.classList.toggle('hidden');
    });

    document.addEventListener('click', (event) => {
        if (!menuButton.contains(event.target) && !dropdownMenu.contains(event.target)) {
            dropdownMenu.classList.add('hidden');
        }
    });

    llmRadios.forEach(radio => {
        radio.addEventListener('change', async (event) => {
            currentLLM = event.target.value;
            localStorage.setItem('lastUsedLLM', currentLLM);
            cachedLeagueData = null;
            lastFetchTime = 0;
            if (currentLeague) {
                await loadLeagueData(currentLeague);
            }
            dropdownMenu.classList.add('hidden');
        });
    });

    document.querySelector('header a[href="#about"]').addEventListener('click', scrollToAbout);
    // Add loaded class to body after all initialization is complete
    document.body.classList.add('loaded');
    // Verify initialization
    console.log('Menu initialization complete, providers:', llmProviders.length);
});

// Navigation functions
const scrollToTop = () => {
    window.scrollTo({top: 0, behavior: 'smooth'});
    return false;
};

const scrollToAbout = event => {
    event.preventDefault();
    document.getElementById('about').scrollIntoView({ behavior: 'smooth' });
};

// Initialize application and set up periodic refresh
createTabs();

// Set up periodic refresh only if we don't have valid cached data
const refreshData = () => {
    const currentTime = Date.now();
    const lastFetch = parseInt(localStorage.getItem('lastFetchTime')) || 0;
    
    if (currentTime - lastFetch > CONFIG.CACHE_DURATION * 1000) {
        if (currentLeague) {
            loadLeagueData(currentLeague);
        }
    }
};

setInterval(refreshData, CONFIG.CACHE_DURATION * 1000);

// Service Worker registration
if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
        navigator.serviceWorker.register('/sw.js')
            .then(registration => console.log('ServiceWorker registration successful'))
            .catch(err => console.log('ServiceWorker registration failed:', err));
    });
}

// Analytics (only in production)
if (window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1') {
    // Vercel Analytics
    window.va = window.va || function () { (window.vaq = window.vaq || []).push(arguments); };
    const vaScript = document.createElement('script');
    vaScript.defer = true;
    vaScript.src = '/_vercel/insights/script.js';
    document.head.appendChild(vaScript);

    // Vercel Speed Insights
    window.si = window.si || function () { (window.siq = window.siq || []).push(arguments); };
    const siScript = document.createElement('script');
    siScript.defer = true;
    siScript.src = '/_vercel/speed-insights/script.js';
    document.head.appendChild(siScript);
}