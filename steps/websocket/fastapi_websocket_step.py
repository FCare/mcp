import asyncio
import json
import logging
import threading
import time
import base64
import uuid
from typing import Optional, Dict, Any, List
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Union, Literal

from pipeline_framework import PipelineStep
from messages.base_message import Message, InputMessage, OutputMessage, ErrorMessage, MessageType

logger = logging.getLogger(__name__)


# Modèles Pydantic pour la documentation des messages WebSocket
class TextMessage(BaseModel):
    """Message texte simple"""
    type: Literal["text"] = "text"
    content: str
    
    class Config:
        schema_extra = {
            "example": {
                "type": "text",
                "content": "Bonjour, comment allez-vous ?"
            }
        }


class AudioMessage(BaseModel):
    """Message audio encodé en base64"""
    type: Literal["audio"] = "audio"
    data: str  # Base64 encoded audio
    metadata: Optional[Dict] = {}
    
    class Config:
        schema_extra = {
            "example": {
                "type": "audio",
                "data": "SGVsbG8gd29ybGQ=",
                "metadata": {"format": "pcm16", "sample_rate": 24000}
            }
        }


class ChatResponse(BaseModel):
    """Réponse de chat du pipeline"""
    type: Literal["chat_response"] = "chat_response"
    content: str
    timestamp: float
    metadata: Dict
    
    class Config:
        schema_extra = {
            "example": {
                "type": "chat_response",
                "content": "Je suis un assistant IA...",
                "timestamp": 1639123456.789,
                "metadata": {"response_type": "partial"}
            }
        }


class AudioChunk(BaseModel):
    """Chunk audio généré par TTS"""
    type: Literal["audio_chunk"] = "audio_chunk"
    data: str  # Base64 encoded audio
    metadata: Dict
    
    class Config:
        schema_extra = {
            "example": {
                "type": "audio_chunk",
                "data": "UkVBTElZX0xPTkdfQVVESU9fREFUQQ==",
                "metadata": {"format": "pcm16", "chunk_id": 1}
            }
        }


class ConnectionEstablished(BaseModel):
    """Message de confirmation de connexion"""
    type: Literal["connection_established"] = "connection_established"
    client_id: str
    mode: str
    timestamp: float
    
    class Config:
        schema_extra = {
            "example": {
                "type": "connection_established",
                "client_id": "abc123ef",
                "mode": "text_to_text",
                "timestamp": 1639123456.789
            }
        }


WebSocketMessage = Union[TextMessage, AudioMessage, ChatResponse, AudioChunk, ConnectionEstablished]


class ConnectionManager:
    """Gestionnaire de connexions WebSocket pour FastAPI"""
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        logger.info(f"✅ Client {client_id} connected. Total connections: {len(self.active_connections)}")
    
    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            logger.info(f"❌ Client {client_id} disconnected. Total connections: {len(self.active_connections)}")
    
    async def send_personal_message(self, message: str, client_id: str):
        """Envoie un message à un client spécifique"""
        if client_id in self.active_connections:
            try:
                await self.active_connections[client_id].send_text(message)
                return True
            except Exception as e:
                logger.error(f"Failed to send message to {client_id}: {e}")
                self.disconnect(client_id)
                return False
        else:
            logger.warning(f"Client {client_id} not found in active connections")
            return False
    
    async def send_audio_to_client(self, client_id: str, audio_data: bytes, metadata: Dict = None):
        """Envoie des données audio à un client"""
        if client_id in self.active_connections:
            try:
                message = {
                    "type": "audio_chunk",
                    "data": base64.b64encode(audio_data).decode('utf-8'),
                    "metadata": metadata or {}
                }
                await self.active_connections[client_id].send_text(json.dumps(message))
                return True
            except Exception as e:
                logger.error(f"Failed to send audio to {client_id}: {e}")
                self.disconnect(client_id)
                return False
        return False
    
    async def broadcast(self, message: str):
        """Diffuse un message à tous les clients connectés"""
        disconnected_clients = []
        for client_id, websocket in self.active_connections.items():
            try:
                await websocket.send_text(message)
            except Exception as e:
                logger.error(f"Failed to broadcast to {client_id}: {e}")
                disconnected_clients.append(client_id)
        
        # Nettoyer les connexions fermées
        for client_id in disconnected_clients:
            self.disconnect(client_id)


class FastAPIWebSocketStep(PipelineStep):
    """
    WebSocket step utilisant FastAPI
    """
    
    def __init__(self, name: str = "FastAPIWebSocketServer", config: Optional[Dict] = None):
        super().__init__(name, config, handler=self._handle_input_message_async)
        
        self.host = config.get("host", "localhost") if config else "localhost"
        self.port = config.get("port", 8765) if config else 8765
        
        # Configuration FastAPI
        self.app = FastAPI(
            title="Master Control Program",
            version="1.0.0",
            description="""
🚀 **Master Control Program (MCP)** - Interface WebSocket avancée pour pipelines IA

## 🔌 Connexions WebSocket
- **ws://localhost:8769/ws** - Connexion automatique avec client_id généré
- **ws://localhost:8769/ws/{client_id}** - Connexion avec ID personnalisé

## 📤 Messages Entrants (Client → MCP)

### Message Texte
```json
{
    "type": "text",
    "content": "Bonjour Master Control Program !"
}
```

### Message Audio
```json
{
    "type": "audio",
    "data": "UkVBTElZX0xPTkdfQVVESU9fREFUQQ==",
    "metadata": {
        "format": "pcm16",
        "sample_rate": 24000
    }
}
```

### Message JSON Complexe
```json
{
    "type": "command",
    "action": "custom_processing",
    "parameters": {"key": "value"}
}
```

## 📥 Messages Sortants (MCP → Client)

### Connexion Établie
```json
{
    "type": "connection_established",
    "client_id": "mcp_user_42abc",
    "mode": "text_to_text",
    "timestamp": 1639123456.789
}
```

### Réponse Chat (Streaming)
```json
{
    "type": "chat_response",
    "content": "Je suis le Master Control Program...",
    "timestamp": 1639123456.789,
    "metadata": {
        "response_type": "partial",
        "model": "gpt-4o-mini"
    }
}
```

### Chunk Audio TTS
```json
{
    "type": "audio_chunk",
    "data": "QVVESU9fQ0hVTktfRVhBTVBMRQ==",
    "metadata": {
        "format": "pcm16",
        "sample_rate": 24000,
        "chunk_id": 1,
        "voice": "en_US-amy-medium"
    }
}
```

### Signal Fin Audio
```json
{
    "type": "audio_finished",
    "total_chunks": 5,
    "total_bytes": 48000,
    "duration_seconds": 2.1,
    "timestamp": 1639123456.789
}
```

### Transcription Audio
```json
{
    "type": "transcription",
    "text": "Bonjour comment allez-vous",
    "confidence": 0.95,
    "timestamp": 1639123456.789
}
```

### Message d'Erreur
```json
{
    "type": "error",
    "message": "Format audio non supporté",
    "code": "INVALID_FORMAT",
    "timestamp": 1639123456.789
}
```

## 🎯 Flux Typique
1. **Connexion** → Réception du `client_id` unique
2. **Message texte** → Streaming `chat_response` partiel
3. **Génération TTS** → Multiple `audio_chunk` → `audio_finished`
4. **Connexion persistante** pour interactions en temps réel
            """,
            contact={
                "name": "Master Control Program",
                "url": "http://localhost:8769/status",
            }
        )
        self.connection_manager = ConnectionManager()
        
        # Mode de fonctionnement
        self.mode = config.get("mode", "text_to_text") if config else "text_to_text"
        self.audio_format = config.get("audio_format", "pcm16") if config else "pcm16"
        self.sample_rate = config.get("sample_rate", 24000) if config else 24000
        
        # Configuration du serveur
        self.server = None
        self.server_thread = None
        self.running = False
        
        # Configurer FastAPI
        self._setup_fastapi()
        
        logger.info(f"FastAPIWebSocketStep '{self.name}' configuré sur {self.host}:{self.port} mode={self.mode}")
    
    def _setup_fastapi(self):
        """Configure FastAPI avec CORS et routes WebSocket"""
        # CORS pour les connexions cross-origin
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        
        # Route WebSocket principale
        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await self._handle_websocket_connection(websocket)
        
        # Route WebSocket avec client_id optionnel
        @self.app.websocket("/ws/{client_id}")
        async def websocket_endpoint_with_id(websocket: WebSocket, client_id: str):
            await self._handle_websocket_connection(websocket, client_id)
        
        # Route de santé
        @self.app.get("/health")
        async def health_check():
            return {
                "status": "healthy",
                "connections": len(self.connection_manager.active_connections),
                "mode": self.mode
            }
        
        # Endpoints utiles seulement
        @self.app.get("/connections",
                     summary="🔌 Connexions Actives",
                     description="Liste des clients connectés au Master Control Program avec leurs client_id",
                     tags=["Control"])
        async def active_connections():
            connections = {}
            for client_id, ws in self.connection_manager.active_connections.items():
                connections[client_id] = {
                    "connected_at": time.time(),
                    "client_type": "websocket",
                    "status": "active"
                }
            return {
                "total_connections": len(connections),
                "connections": connections,
                "websocket_endpoints": {
                    "/ws": "Auto-generated client_id",
                    "/ws/{client_id}": "Custom client_id"
                }
            }
        
        @self.app.get("/status",
                     summary="⚡ Statut MCP",
                     description="Statut détaillé du Master Control Program",
                     tags=["Control"])
        async def mcp_status():
            return {
                "system": "Master Control Program",
                "version": "1.0.0",
                "mode": self.mode,
                "host": self.host,
                "port": self.port,
                "connections": len(self.connection_manager.active_connections),
                "endpoints": {
                    "websocket": f"ws://{self.host}:{self.port}/ws",
                    "websocket_custom": f"ws://{self.host}:{self.port}/ws/{{client_id}}",
                    "docs": f"http://{self.host}:{self.port}/docs",
                    "health": f"http://{self.host}:{self.port}/health"
                },
                "capabilities": [
                    "real_time_chat",
                    "audio_processing",
                    "tts_generation",
                    "streaming_responses",
                    "multi_client_support",
                    "client_id_tracking"
                ]
            }
    
    async def _handle_websocket_connection(self, websocket: WebSocket, client_id: Optional[str] = None):
        """Gère une nouvelle connexion WebSocket"""
        if not client_id:
            client_id = str(uuid.uuid4())[:8]
        
        try:
            await self.connection_manager.connect(websocket, client_id)
            
            # Envoyer un message de bienvenue
            welcome_message = {
                "type": "connection_established",
                "client_id": client_id,
                "mode": self.mode,
                "timestamp": time.time()
            }
            await websocket.send_text(json.dumps(welcome_message))
            
            # Boucle de réception des messages
            while True:
                try:
                    data = await websocket.receive_text()
                    await self._process_incoming_message(data, client_id)
                except WebSocketDisconnect:
                    logger.info(f"Client {client_id} disconnected normally")
                    break
                except Exception as e:
                    logger.error(f"Error processing message from {client_id}: {e}")
                    break
        
        except Exception as e:
            logger.error(f"WebSocket connection error for {client_id}: {e}")
        finally:
            self.connection_manager.disconnect(client_id)
    
    async def _process_incoming_message(self, data: str, client_id: str):
        """Traite un message entrant d'un client WebSocket"""
        try:
            # Parser le message JSON
            try:
                message_data = json.loads(data)
                message_type = message_data.get('type', 'text')
            except json.JSONDecodeError:
                # Si ce n'est pas du JSON, traiter comme texte simple
                message_data = {"type": "text", "content": data}
                message_type = "text"
            
            logger.info(f"Received {message_type} message from {client_id}")
            
            # Créer un message pour le pipeline avec métadonnées
            metadata = {
                "client_id": client_id,
                "original_client_id": client_id,
                "message_type": message_type,
                "timestamp": time.time()
            }
            
            # Traitement selon le type de message
            if message_type == "text":
                content = message_data.get('content', message_data.get('text', data))
                pipeline_message = InputMessage(data=content, metadata=metadata)
                
            elif message_type == "audio":
                # Traitement des données audio (base64 encodées)
                audio_data = message_data.get('data', '')
                try:
                    decoded_audio = base64.b64decode(audio_data)
                    pipeline_message = InputMessage(data=decoded_audio, metadata=metadata)
                except Exception as e:
                    logger.error(f"Failed to decode audio data: {e}")
                    return
            else:
                # Autres types de messages
                pipeline_message = InputMessage(data=message_data, metadata=metadata)
            
            # Envoyer au pipeline via output_queue (vers le step suivant)
            if self.output_queue:
                self.output_queue.enqueue(pipeline_message)
                logger.info(f"Message from {client_id} sent to pipeline")
            else:
                logger.warning("No output_queue configured, message discarded")
        
        except Exception as e:
            logger.error(f"Error processing message from {client_id}: {e}")
            # Envoyer un message d'erreur au client
            error_message = {
                "type": "error",
                "message": f"Failed to process message: {str(e)}",
                "timestamp": time.time()
            }
            await self.connection_manager.send_personal_message(
                json.dumps(error_message), client_id
            )
    
    async def _handle_input_message_async(self, message_data):
        """Handler async pour traiter les réponses des autres steps"""
        try:
            logger.info(f"FastAPI WebSocket received message: type={type(message_data).__name__}")
            
            # Gestion des messages de contrôle (audio_finished, etc.)
            if isinstance(message_data, dict) and message_data.get('type') == 'audio_finished':
                logger.info(f"Broadcasting audio_finished signal to all connected clients")
                finish_message = json.dumps({
                    "type": "audio_finished",
                    "total_chunks": message_data.get('total_chunks', 0),
                    "total_bytes": message_data.get('total_bytes', 0),
                    "duration_seconds": message_data.get('duration_seconds', 0),
                    "timestamp": time.time()
                })
                await self.connection_manager.broadcast(finish_message)
                return
            
            if hasattr(message_data, 'data'):
                data = message_data.data
                metadata = getattr(message_data, 'metadata', {})
                
                # Extraire le client_id original
                original_client_id = metadata.get('original_client_id')
                if not original_client_id:
                    logger.warning(f"No original_client_id in metadata, cannot route response: {metadata}")
                    return
                
                message_type = metadata.get('type', 'unknown')
                
                if message_type == 'audio_chunk' and isinstance(data, bytes):
                    # Message audio
                    logger.info(f"Sending audio chunk to client {original_client_id}: {len(data)} bytes")
                    await self.connection_manager.send_audio_to_client(original_client_id, data, metadata)
                    
                elif message_type == 'audio_finished':
                    # Signal de fin audio
                    logger.info(f"Sending audio finished signal to client {original_client_id}")
                    finish_message = {
                        "type": "audio_finished",
                        "total_chunks": data.get('total_chunks', 0) if isinstance(data, dict) else 0,
                        "total_bytes": data.get('total_bytes', 0) if isinstance(data, dict) else 0,
                        "timestamp": time.time(),
                        "metadata": metadata
                    }
                    await self.connection_manager.send_personal_message(
                        json.dumps(finish_message), original_client_id
                    )
                    
                elif isinstance(data, (str, int, float)):
                    # Message texte - format chat response
                    logger.info(f"Sending chat response to client {original_client_id}: '{str(data)[:50]}{'...' if len(str(data)) > 50 else ''}'")
                    response_message = {
                        "type": "chat_response",
                        "content": str(data),
                        "timestamp": time.time(),
                        "metadata": metadata
                    }
                    await self.connection_manager.send_personal_message(
                        json.dumps(response_message), original_client_id
                    )
                else:
                    # Autres types de données
                    logger.info(f"Sending generic response to client {original_client_id}")
                    response_message = {
                        "type": "response",
                        "content": str(data),
                        "timestamp": time.time(),
                        "metadata": metadata
                    }
                    await self.connection_manager.send_personal_message(
                        json.dumps(response_message), original_client_id
                    )
        
        except Exception as e:
            logger.error(f"Error handling input message: {e}")
    
    def init(self) -> bool:
        """Démarre le serveur FastAPI dans un thread séparé"""
        try:
            self.running = True
            self.server_thread = threading.Thread(target=self._run_server, daemon=True)
            self.server_thread.start()
            
            # Attendre que le serveur soit prêt
            for i in range(50):
                if self.server is not None:
                    logger.info(f"FastAPI WebSocket server started on {self.host}:{self.port}")
                    return True
                time.sleep(0.1)
            
            logger.error("Failed to start FastAPI server within timeout")
            return False
            
        except Exception as e:
            logger.error(f"Error starting FastAPI server: {e}")
            return False
    
    def _run_server(self):
        """Lance le serveur FastAPI/uvicorn dans sa propre boucle d'événements"""
        try:
            # Configuration uvicorn
            config = uvicorn.Config(
                app=self.app,
                host=self.host,
                port=self.port,
                log_level="info",
                access_log=False  # Réduire le spam des logs
            )
            self.server = uvicorn.Server(config)
            
            # Démarrer le serveur
            logger.info(f"Starting FastAPI server on {self.host}:{self.port}")
            asyncio.run(self.server.serve())
            
        except Exception as e:
            logger.error(f"FastAPI server error: {e}")
        finally:
            self.running = False
    
    def cleanup(self):
        """Arrête le serveur et nettoie les ressources"""
        try:
            logger.info("Shutting down FastAPI WebSocket server...")
            self.running = False
            
            if self.server:
                self.server.should_exit = True
            
            # Fermer toutes les connexions WebSocket
            if hasattr(self, 'connection_manager'):
                for client_id in list(self.connection_manager.active_connections.keys()):
                    self.connection_manager.disconnect(client_id)
            
            logger.info("FastAPI WebSocket server cleanup completed")
            
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")