import React, { useState, useEffect } from 'react';
import axios from 'axios';
import './App.css';

const fallbackQuestions = [
  {
    question: "What is a common challenge when migrating SAS to Python?",
    options: [
      "Syntax differences",
      "Data type mapping",
      "Library availability",
      "All of the above"
    ],
    correct_answer: "All of the above",
    explanation: "SAS and Python have different syntax, data type handling, and available libraries, all of which can pose challenges during migration.",
    source: "fallback"
  },
  {
    question: "Which Python library is often used for data manipulation and analysis, similar to SAS procedures?",
    options: [
      "NumPy",
      "Pandas",
      "Matplotlib",
      "Scikit-learn"
    ],
    correct_answer: "Pandas",
    explanation: "Pandas provides DataFrames, which are powerful and flexible for data manipulation and analysis, comparable to SAS datasets and procedures.",
    source: "fallback"
  }
];

function RuleBasedConverter() {
  const [sasFile, setSasFile] = useState(null);
  const [pyCode, setPyCode] = useState('');
  const [summary, setSummary] = useState('');
  const [unsupported, setUnsupported] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');

  const handleFileChange = (event) => {
    setSasFile(event.target.files[0]);
    resetResults();
  };

  const resetResults = () => {
    setPyCode('');
    setSummary('');
    setUnsupported([]);
    setError('');
  };

  const handleSubmit = async () => {
    if (!sasFile) {
      setError('Please upload a SAS file.');
      return;
    }

    setIsLoading(true);
    setError('');
    resetResults();

    const formData = new FormData();
    formData.append('file', sasFile);

    try {
      const response = await axios.post('/api/convert/sas-to-python/rule-based', formData, {
        headers: {
          'Content-Type': 'multipart/form-data'
        }
      });
      setPyCode(response.data.python_code);
      setSummary(response.data.summary);
      setUnsupported(response.data.unsupported_statements);
    } catch (err) {
      setError('An error occurred during conversion. Please try again.');
      console.error('Conversion error:', err);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="form-section">
      <h2>Rule-Based SAS to Python Converter</h2>
      <div className="form-field">
        <label>Upload SAS File:</label>
        <div className="file-input-wrapper">
          <input type="file" accept=".sas" onChange={handleFileChange} />
          <button className="file-input-button">Choose File</button>
        </div>
        {sasFile && <span style={{ marginLeft: '10px' }}>{sasFile.name}</span>}
      </div>
      <button onClick={handleSubmit} className="submit-button" disabled={isLoading || !sasFile}>
        {isLoading ? 'Converting...' : 'Convert to Python'}
      </button>

      {error && <div className="error-message">{error}</div>}

      {isLoading && (
        <div className="loading-message">
          <div className="spinner"></div>
          <span>Converting...</span>
        </div>
      )}

      {!isLoading && (pyCode || summary || unsupported.length > 0) && (
        <div className="results-section">
          <h3>Generated Python Code:</h3>
          {pyCode ? (
            <pre className="code-block">{pyCode}</pre>
          ) : (
            <p>No Python code generated.</p>
          )}

          <h3>Migration Summary:</h3>
          {summary ? (
            <p className="summary-text">{summary}</p>
          ) : (
            <p>No summary available.</p>
          )}

          <h3>Unsupported Statements:</h3>
          {unsupported.length > 0 ? (
            <ul className="unsupported-statements-list">
              {unsupported.map((statement, index) => (
                <li key={index}>{statement}</li>
              ))}
            </ul>
          ) : (
            <p>All statements were supported.</p>
          )}
        </div>
      )}
    </div>
  );
}

function AIAgentConverter() {
  const [sasFile, setSasFile] = useState(null);
  const [llmProviders, setLlmProviders] = useState([]);
  const [selectedProvider, setSelectedProvider] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [generatedPythonCode, setGeneratedPythonCode] = useState('');
  const [migrationReport, setMigrationReport] = useState('');
  const [executionTimeline, setExecutionTimeline] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const fetchProviders = async () => {
      try {
        const response = await axios.get('/api/llm/providers');
        setLlmProviders(response.data.providers);
        if (response.data.providers.length > 0) {
          setSelectedProvider(response.data.providers[0]);
        }
      } catch (err) {
        console.error('Failed to fetch LLM providers:', err);
        setError('Could not load LLM providers. Please try again later.');
      }
    };
    fetchProviders();
  }, []);

  const handleFileChange = (event) => {
    setSasFile(event.target.files[0]);
    resetResults();
  };

  const handleProviderChange = (event) => {
    setSelectedProvider(event.target.value);
    resetResults();
  };

  const handleApiKeyChange = (event) => {
    setApiKey(event.target.value);
  };

  const resetResults = () => {
    setGeneratedPythonCode('');
    setMigrationReport('');
    setExecutionTimeline('');
    setError('');
  };

  const handleSubmit = async () => {
    if (!sasFile) {
      setError('Please upload a SAS file.');
      return;
    }
    if (!selectedProvider) {
      setError('Please select an LLM provider.');
      return;
    }
    if (!apiKey) {
      setError('Please enter your API key.');
      return;
    }

    setIsLoading(true);
    setError('');
    resetResults();

    const formData = new FormData();
    formData.append('file', sasFile);
    formData.append('llm_provider', selectedProvider);
    formData.append('api_key', apiKey);

    try {
      const response = await axios.post('/api/convert/sas-to-python/ai-agent', formData, {
        headers: {
          'Content-Type': 'multipart/form-data'
        }
      });
      setGeneratedPythonCode(response.data.python_code);
      setMigrationReport(response.data.migration_report);
      setExecutionTimeline(response.data.execution_timeline);
    } catch (err) {
      setError('An error occurred during AI conversion. Please check your API key and try again.');
      console.error('AI Conversion error:', err);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="form-section">
      <h2>AI Agent-Based SAS to Python Converter</h2>
      <div className="form-field">
        <label>Upload SAS File:</label>
        <div className="file-input-wrapper">
          <input type="file" accept=".sas" onChange={handleFileChange} />
          <button className="file-input-button">Choose File</button>
        </div>
        {sasFile && <span style={{ marginLeft: '10px' }}>{sasFile.name}</span>}
      </div>
      <div className="llm-provider-selector">
        <label htmlFor="llm-provider">LLM Provider:</label>
        <select id="llm-provider" value={selectedProvider} onChange={handleProviderChange}>
          {llmProviders.length > 0 ? (
            llmProviders.map(provider => (
              <option key={provider} value={provider}>{provider}</option>
            ))
          ) : (
            <option value="">Loading providers...</option>
          )}
        </select>
      </div>
      <div className="form-field">
        <label htmlFor="api-key">API Key:</label>
        <input type="password" id="api-key" value={apiKey} onChange={handleApiKeyChange} placeholder="Enter your LLM API Key" />
      </div>
      <button onClick={handleSubmit} className="submit-button" disabled={isLoading || !sasFile || !selectedProvider || !apiKey}>
        {isLoading ? 'Converting...' : 'Convert to Python with AI'}
      </button>

      {error && <div className="error-message">{error}</div>}

      {isLoading && (
        <div className="loading-message">
          <div className="spinner"></div>
          <span>Converting with AI...</span>
        </div>
      )}

      {!isLoading && (generatedPythonCode || migrationReport || executionTimeline) && (
        <div className="results-section">
          <h3>Generated Python Code:</h3>
          {generatedPythonCode ? (
            <pre className="code-block">{generatedPythonCode}</pre>
          ) : (
            <p>No Python code generated.</p>
          )}

          <h3>Migration Report:</h3>
          {migrationReport ? (
            <p className="migration-report-text">{migrationReport}</p>
          ) : (
            <p>No migration report available.</p>
          )}

          <h3>Execution Timeline:</h3>
          {executionTimeline ? (
            <p className="execution-timeline-text">{executionTimeline}</p>
          ) : (
            <p>No execution timeline available.</p>
          )}
        </div>
      )}
    </div>
  );
}

function QuizTab() {
  const [questions, setQuestions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedAnswers, setSelectedAnswers] = useState({});
  const [showResults, setShowResults] = useState(false);

  useEffect(() => {
    const fetchQuestions = async () => {
      setLoading(true);
      setError('');
      try {
        // The plan states to fetch questions and use them immediately for grading.
        // No specific endpoint for quiz questions is provided, so we'll simulate fetching for now.
        // In a real app, this would be a GET request to an endpoint like '/api/quiz/sas-to-python'
        // For this example, we'll directly use a fallback for demonstration.
        
        // Simulate network delay
        await new Promise(resolve => setTimeout(resolve, 500));
        setQuestions(fallbackQuestions);
        
      } catch (err) {
        setError('Failed to load quiz questions. Please try again later.');
        setQuestions(fallbackQuestions); // Use fallback on error
      } finally {
        setLoading(false);
      }
    };
    fetchQuestions();
  }, []);

  const handleAnswerSelect = (questionIndex, answer) => {
    setSelectedAnswers(prev => ({ ...prev, [questionIndex]: answer }));
    if (!showResults) {
      setShowResults(true); // Show results immediately after first answer selection
    }
  };

  const isCorrect = (questionIndex, answer) => {
    return questions[questionIndex]?.correct_answer === answer;
  };

  const getOptionClass = (questionIndex, option) => {
    if (!showResults) return '';
    const selected = selectedAnswers[questionIndex] === option;
    const correct = questions[questionIndex]?.correct_answer === option;
    if (selected) {
      return isCorrect(questionIndex, option) ? 'correct' : 'incorrect';
    }
    if (correct) {
      return 'correct';
    }
    return '';
  };

  return (
    <div className="form-section">
      <h2>SAS to Python Migration Quiz</h2>
      {loading && (
        <div className="loading-message">
          <div className="spinner"></div>
          <span>Loading Quiz...</span>
        </div>
      )}
      {error && <div className="error-message">{error}</div>}
      {!loading && !error && questions.length === 0 && (
        <div className="empty-message">No quiz questions available.</div>
      )}
      {!loading && !error && questions.length > 0 && (
        <div>
          {questions.map((q, index) => (
            <div key={index} className="question-card">
              <h3>Question {index + 1}:</h3>
              <p>{q.question}</p>
              <div className="options-container">
                {q.options.map((option, optIndex) => (
                  <button
                    key={optIndex}
                    className={`option-button ${getOptionClass(index, option)}`}
                    onClick={() => handleAnswerSelect(index, option)}
                    disabled={selectedAnswers.hasOwnProperty(index)}
                  >
                    {option}
                  </button>
                ))}
              </div>
              {showResults && selectedAnswers.hasOwnProperty(index) && (
                <div className="explanation-container">
                  <p>
                    <strong>Your Answer:</strong> {selectedAnswers[index]}
                  </p>
                  <p>
                    <strong>Correct Answer:</strong> {q.correct_answer}
                  </p>
                  <p className="explanation-text">
                    <strong>Explanation:</strong> {q.explanation}
                  </p>
                  {q.source && <p className="source-text">Source: {q.source}</p>}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function App() {
  const [activeTab, setActiveTab] = useState('rule-based');

  return (
    <div className="app-container">
      <header className="header">
        <h1>SAS2Python AI Converter</h1>
        <p>Effortlessly migrate your SAS code to Python.</p>
      </header>

      <nav className="tab-bar">
        <button
          className={`tab-button ${activeTab === 'rule-based' ? 'active' : ''}`}
          onClick={() => setActiveTab('rule-based')}
        >
          Rule-Based Converter
        </button>
        <button
          className={`tab-button ${activeTab === 'ai-agent' ? 'active' : ''}`}
          onClick={() => setActiveTab('ai-agent')}
        >
          AI Agent Converter
        </button>
        <button
          className={`tab-button ${activeTab === 'quiz' ? 'active' : ''}`}
          onClick={() => setActiveTab('quiz')}
        >
          Migration Quiz
        </button>
      </nav>

      <main>
        {activeTab === 'rule-based' && <RuleBasedConverter />}
        {activeTab === 'ai-agent' && <AIAgentConverter />}
        {activeTab === 'quiz' && <QuizTab />}
      </main>
    </div>
  );
}

export default App;
