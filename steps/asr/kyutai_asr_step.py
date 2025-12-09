import time
import urllib.parse
import os
import sys
import logging
import math
import struct
from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, Any

from pipeline_framework import PipelineStep
from messages.base_message import Message, InputMessage, OutputMessage, ErrorMessage, MessageType
from utils.chunk_queue import ChunkQueue

try:
    import websocket
    import msgpack
except ImportError as e:
    print(f"""
    Missing dependencies for MoshiASR: {e}
    Install with: pip install websocket-client msgpack
    """)
    sys.exit(1)

logger = logging.getLogger(__name__)

# Constantes ASR
SAMPLE_RATE = 24000
SAMPLES_PER_FRAME = 1920
FRAME_TIME_SEC = SAMPLES_PER_FRAME / SAMPLE_RATE
PAUSE_THRESHOLD = 0.9
VAD_THRESHOLD = 0.8
FLUSH_LENGTH = 12


class ExponentialMovingAverage:
    """Exponential Moving Average for pause prediction smoothing."""
    def __init__(self, attack_time: float, release_time: float, initial_value: float = 0.0):
        self.attack_time = attack_time
        self.release_time = release_time
        self.value = initial_value

    def update(self, dt: float, new_value: float) -> float:
        if new_value > self.value:
            alpha = 1 - math.exp(-dt / self.attack_time * math.log(2))
        else:
            alpha = 1 - math.exp(-dt / self.release_time * math.log(2))
        
        self.value = float((1 - alpha) * self.value + alpha * new_value)
        return self.value


class ASREventType(Enum):
    """Types d'événements ASR"""
    AUDIO = "audio"    # Input: chunk audio
    TEXT = "text"      # Output: transcription
    START = "start"    # Output: voice activity
    END = "end"        # Output: pause détectée


@dataclass
class ASREvent:
    """Événement ASR standardisé pour input/output"""
    type: ASREventType
    data: Any = None
    timestamp: Optional[float] = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()


@dataclass
class TextEvent(ASREvent):
    """Événement output texte"""
    text: str = ""
    type: ASREventType = None
    
    def __post_init__(self):
        if self.type is None:
            self.type = ASREventType.TEXT
        super().__post_init__()
        self.data = self.text


@dataclass
class StartEvent(ASREvent):
    """Événement voix détectée"""
    reason: str = "voice_detected"
    type: ASREventType = None
    
    def __post_init__(self):
        if self.type is None:
            self.type = ASREventType.START
        super().__post_init__()
        self.data = {"reason": self.reason}


@dataclass
class EndEvent(ASREvent):
    """Événement fin/pause détectée"""
    reason: str = "pause_detected" 
    type: ASREventType = None
    
    def __post_init__(self):
        if self.type is None:
            self.type = ASREventType.END
        super().__post_init__()
        self.data = {"reason": self.reason}


class MoshiASR:
    """
    ASR implementation using Moshi STT server via synchronous WebSocket.
    Intégré avec ChunkQueue pour éliminer les threads manuels.
    """

    def __init__(self, host: str, **params):
        self.host = host
        self.name = f"MoshiASR({self.host or 'auto'})"

        self.ws = None
        self._connected = False
        self._stream_active = False
        
        self.is_speaking = False
        self.last_word_time = 0.0
        
        self.pause_prediction = ExponentialMovingAverage(
            attack_time=0.01, release_time=0.01, initial_value=1.0
        )
        self.steps_to_wait = 10

        self.pause_threshold = PAUSE_THRESHOLD
        self.vad_threshold = VAD_THRESHOLD

        self.flushing_mode = False
        self.silence_packets_count = FLUSH_LENGTH
        self.flushing_limit = 0
        
        self.packets_sent = 0
        self.packets_received = 0
        
        # Output queue sera la ChunkQueue
        self.output_queue = None

        logger.debug(f"{self.name}: Initialized with ChunkQueue architecture")

    def set_output_queue(self, queue):
        """Configure la queue de sortie (ChunkQueue)"""
        self.output_queue = queue

    def connect(self):
        """Establish WebSocket connection with Moshi STT server."""
        if self._connected:
            return
        
        ws_url = self._build_websocket_url()
        logger.debug(f"{self.name}: Connecting to {ws_url}")
        
        self.ws = websocket.WebSocketApp(
            url=ws_url,
            header=[f"kyutai-api-key: public_token"],
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        
        def start_ws():
            import ssl
            self.ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        
        import threading
        threading.Thread(target=start_ws, daemon=True).start()
        
        timeout = 10.0
        start_time = time.time()
        while not self._connected and (time.time() - start_time) < timeout:
            time.sleep(0.1)
            
        if not self._connected:
            raise RuntimeError(f"Failed to connect to {ws_url} within {timeout}s")

    def _build_websocket_url(self):
        """Build WebSocket URL for STT connection."""
        protocol = "wss"
        base_path = "/stt"
        return f"{protocol}://{self.host}{base_path}"

    def on_open(self, ws):
        """WebSocket connection opened callback."""
        self._connected = True
        logger.debug(f"{self.name}: WebSocket connected successfully")

    def on_message(self, ws, message):
        """WebSocket message received callback."""
        try:
            message_dict = msgpack.unpackb(message)
            
            if message_dict.get("type") == "Ready":
                self._stream_active = True
                return
            
            message_type = message_dict.get('type', 'unknown')

            if message_type == 'Word':
                word_text = message_dict.get('text', '')
                start_time = message_dict.get('start_time', 0)
                if self.flushing_mode:
                    self.flushing_mode = False
                    logger.debug("Cancel flushing mode")
                    if not self.is_speaking:
                        self._enqueue_event(StartEvent())
                
                self.pause_prediction.value = 0.0
                
                if self.output_queue:
                    self._enqueue_event(TextEvent(text=word_text))
                
                self.last_word_time = start_time
                self.is_speaking = True
                
                logger.debug(f"{self.name}: Word: '{word_text}' at {start_time:.2f}s")
                
            elif message_type == 'EndWord':
                stop_time = message_dict.get('stop_time', 0)
                logger.debug(f"{self.name}: Word ended at {stop_time:.2f}s")
                
            elif message_type == 'Marker':
                marker_id = message_dict.get('id', 0)
                logger.debug(f"{self.name}: Marker received: {marker_id}")
                
            elif message_type == 'Step':
                step_idx = message_dict.get('step_idx', 0)
                prs = message_dict.get('prs', [])

                self.packets_received += 1

                if len(prs) >= 3:
                    if self.steps_to_wait > 0:
                        self.steps_to_wait -= 1
                        return
                    
                    self.pause_prediction.update(FRAME_TIME_SEC, prs[2])
        
                    if self.pause_prediction.value > self.pause_threshold and self.is_speaking:
                        if not self.flushing_mode:
                            logger.debug("Starting flushing mode")
                            self._enter_flushing_mode()
                    
                    elif self.pause_prediction.value < self.vad_threshold:
                        self.pause_prediction.value = 0.0
                        if self.flushing_mode:
                            logger.debug("Cancel flushing mode")
                            self.flushing_mode = False
                        if not self.is_speaking:
                            self._enqueue_event(StartEvent())
                            
            if self.flushing_mode:
                if self.flushing_limit == self.packets_received:
                    logger.debug("Flushing Done")
                    self.is_speaking = False
                    self.flushing_mode = False
                    if self.output_queue:
                        logger.debug("Flushing Done : Trigger LLM")
                        self._enqueue_event(EndEvent())
                    return
                
        except Exception as e:
            logger.error(f"{self.name}: Error processing message: {e}")

    def _enqueue_event(self, event: ASREvent):
        """Envoie un événement via ChunkQueue"""
        if self.output_queue:
            self.output_queue.enqueue(event)

    def on_error(self, ws, error):
        """WebSocket error callback."""
        logger.error(f"{self.name}: WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        """WebSocket close callback."""
        self._connected = False
        self._stream_active = False
        logger.debug(f"{self.name}: WebSocket disconnected")

    def _send_audio(self, audio_data: list, timestamp: float):
        """Send audio packet directly to STT server."""
        if not self._connected or not self.ws:
            return
        try:
            message = {
                "type": "Audio",
                "timestamp": timestamp,
                "pcm": audio_data if isinstance(audio_data, list) else list(audio_data)
            }
            
            packed_message = msgpack.packb(message, use_bin_type=True, use_single_float=True)
            self.ws.send(packed_message, opcode=websocket.ABNF.OPCODE_BINARY)
            self.packets_sent += 1
            
        except Exception as e:
            logger.error(f"{self.name}: Error sending packet to STT: {e}")

    def _enter_flushing_mode(self):
        """Enter flushing mode and send silence packets."""
        self.flushing_mode = True
        
        silence_samples = [0.0] * SAMPLES_PER_FRAME
        self.flushing_limit = self.packets_sent + self.silence_packets_count
        for i in range(self.silence_packets_count):
            try:
                timestamp = time.time()
                self._send_audio(silence_samples, timestamp)
            except Exception as e:
                logger.error(f"{self.name}: Error sending silence packet {i}: {e}")
                break

    def _process_audio_chunk(self, audio_chunk: str):
        """
        Main entry point - direct synchronous sending.
        
        Receives audio from client → Split into 80ms → Send directly to STT
        """
        if not self._connected or not self._stream_active:
            logger.debug("not active")
            return
            
        try:
            # Decode binary audio data using struct.unpack
            num_samples = len(audio_chunk) // 2  # 2 bytes per int16
            audio_ints = struct.unpack(f'<{num_samples}h', audio_chunk)
            
            # Convert int16 to float32 and normalize
            audio_float32 = [float(sample) / 32767.0 for sample in audio_ints]
            
            base_timestamp = time.time()
            packet_index = 0
            
            for i in range(0, len(audio_float32), SAMPLES_PER_FRAME):
                chunk = audio_float32[i:i + SAMPLES_PER_FRAME]
                
                if len(chunk) < SAMPLES_PER_FRAME:
                    logger.warning(f"Audio packet should be multiple of {SAMPLES_PER_FRAME}")
                
                packet_timestamp = base_timestamp + (packet_index * FRAME_TIME_SEC)
                self._send_audio(chunk, packet_timestamp)
                packet_index += 1
            
        except Exception as e:
            logger.error(f"{self.name}: Error processing audio chunk: {e}")

    def disconnect(self):
        """Clean disconnection."""
        logger.debug(f"{self.name}: Disconnecting...")
        
        try:
            if self.ws:
                try:
                    self.ws.close()
                except Exception as e:
                    logger.error(f"{self.name}: Error while closing websocket - {e}")
            
        except Exception as e:
            logger.error(f"{self.name}: Disconnect error: {e}")
        finally:
            self._connected = False
            self._stream_active = False
            self.flushing_mode = False
            self.packets_sent = 0
            self.packets_received = 0
            logger.debug(f"{self.name}: Disconnected successfully")

    def reset(self):
        """Reset the user input buffer and transcription state."""
        try:
            self.is_speaking = False
            self.pause_prediction.value = 1.0
            self.flushing_limit = 0
            self.flushing_mode = False
            
            logger.debug(f"{self.name}: Reset completed")
            
        except Exception as e:
            logger.error(f"{self.name}: Reset error: {e}")


class KyutaiASRStep(PipelineStep):
    """
    Step ASR utilisant Moshi avec ChunkQueue pour éliminer les threads manuels.
    """
    
    def __init__(self, name: str = "KyutaiASR", config: Optional[Dict] = None):
        super().__init__(name, config)
        
        self.host = config.get("host", "stt.kyutai.org") if config else "stt.kyutai.org"
        self.port = config.get("port", 443) if config else 443
        self.api_key = config.get("api_key", "public_token") if config else "public_token"
        
        self.sample_rate = config.get("sample_rate", 24000) if config else 24000
        self.samples_per_frame = config.get("samples_per_frame", 1920) if config else 1920
        
        self.pause_threshold = config.get("pause_threshold", 0.9) if config else 0.9
        self.vad_threshold = config.get("vad_threshold", 0.8) if config else 0.8
        
        self.moshi_asr = None
        self.text_buffer = []
        self.current_client_id = None
        
        # ChunkQueue pour traiter les événements Moshi
        self.moshi_event_queue = ChunkQueue(handler=self._handle_moshi_event)
        
        print(f"KyutaiASRStep '{self.name}' configuré pour {self.host}")
    
    def init(self) -> bool:
        """Initialise l'ASR Kyutai"""
        try:
            print(f"Initialisation Kyutai ASR sur {self.host}")
            
            # Crée l'instance MoshiASR
            self.moshi_asr = MoshiASR(host=self.host)
            
            # Configure la ChunkQueue comme output de MoshiASR
            self.moshi_asr.set_output_queue(self.moshi_event_queue)
            
            # Connexion WebSocket
            self.moshi_asr.connect()
            
            print(f"Kyutai ASR initialisé avec succès")
            return True
            
        except Exception as e:
            print(f"Erreur initialisation Kyutai ASR: {e}")
            logger.error(f"Kyutai ASR init error: {e}")
            return False
    
    def _handle_moshi_event(self, moshi_event: ASREvent):
        """Traite un événement reçu de MoshiASR via ChunkQueue"""
        try:
            if moshi_event.type == ASREventType.TEXT:
                # Texte transcrit reçu
                text = moshi_event.data
                if text:
                    self._handle_transcribed_text(text)
            
            elif moshi_event.type == ASREventType.START:
                # Début de parole détecté
                self._handle_speech_start()
            
            elif moshi_event.type == ASREventType.END:
                # Fin de parole détectée
                self._handle_speech_end()
            
            else:
                logger.debug(f"Événement Moshi non géré: {moshi_event.type}")
        
        except Exception as e:
            logger.error(f"Erreur handling événement Moshi: {e}")
    
    def _handle_transcribed_text(self, text: str):
        """Traite le texte transcrit"""
        try:
            # Ajoute au buffer de texte
            self.text_buffer.append(text)
            
            # Prépare le message de sortie
            transcription_message = OutputMessage(
                result=text,
                metadata={
                    "original_client_id": self.current_client_id,
                    "transcription_type": "partial",
                    "buffer_length": len(self.text_buffer),
                    "timestamp": time.time()
                }
            )
            
            # Envoie vers la queue de sortie
            if self.output_queue:
                try:
                    import asyncio
                    if asyncio.iscoroutine(self.output_queue.put):
                        # Asyncio queue
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.create_task(self.output_queue.put(transcription_message))
                        else:
                            loop.run_until_complete(self.output_queue.put(transcription_message))
                    else:
                        # Queue normale
                        self.output_queue.put_nowait(transcription_message)
                    logger.debug(f"Texte transcrit envoyé: '{text}'")
                except Exception as e:
                    logger.error(f"Erreur envoi transcription: {e}")
            
        except Exception as e:
            logger.error(f"Erreur traitement texte transcrit: {e}")
    
    def _handle_speech_start(self):
        """Traite le début de parole"""
        try:
            logger.debug("Début de parole détecté")
            
            # Réinitialise le buffer
            self.text_buffer = []
            
            # Envoie un événement de début
            start_message = OutputMessage(
                result="",
                metadata={
                    "original_client_id": self.current_client_id,
                    "event_type": "speech_start",
                    "timestamp": time.time()
                }
            )
            
            self._send_output_message(start_message)
            
        except Exception as e:
            logger.error(f"Erreur traitement début parole: {e}")
    
    def _handle_speech_end(self):
        """Traite la fin de parole"""
        try:
            logger.debug("Fin de parole détectée")
            
            # Compile le texte final
            final_text = " ".join(self.text_buffer).strip()
            
            if final_text:
                # Envoie la transcription finale
                final_message = OutputMessage(
                    result=final_text,
                    metadata={
                        "original_client_id": self.current_client_id,
                        "transcription_type": "final",
                        "word_count": len(final_text.split()),
                        "timestamp": time.time()
                    }
                )
                
                self._send_output_message(final_message)
                logger.info(f"Transcription finale: '{final_text}'")
            
            # Envoie un événement de fin
            end_message = OutputMessage(
                result="",
                metadata={
                    "original_client_id": self.current_client_id,
                    "event_type": "speech_end",
                    "final_text": final_text,
                    "timestamp": time.time()
                }
            )
            
            self._send_output_message(end_message)
            
        except Exception as e:
            logger.error(f"Erreur traitement fin parole: {e}")

    def _send_output_message(self, message: OutputMessage):
        """Envoie un message vers la queue de sortie"""
        if self.output_queue:
            try:
                import asyncio
                if asyncio.iscoroutine(self.output_queue.put):
                    # Asyncio queue
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.create_task(self.output_queue.put(message))
                    else:
                        loop.run_until_complete(self.output_queue.put(message))
                else:
                    # Queue normale
                    self.output_queue.put_nowait(message)
            except Exception as e:
                logger.error(f"Erreur envoi message: {e}")
    
    def process_message(self, message: Message) -> Optional[Message]:
        """
        Traite un message contenant un chunk audio
        """
        try:
            if message.type != MessageType.INPUT:
                return None
            
            # Récupère les données audio
            audio_data = message.data
            if not audio_data:
                return ErrorMessage(error_message="Pas de données audio")
            
            # Récupère l'ID du client pour le routage de retour
            if message.metadata:
                self.current_client_id = message.metadata.get("client_id")
            
            # Vérifie que MoshiASR est initialisé
            if not self.moshi_asr:
                return ErrorMessage(error_message="MoshiASR non initialisé")
            
            # Traite le chunk audio avec MoshiASR
            if isinstance(audio_data, bytes):
                # Audio binaire brut - utilise la méthode interne de MoshiASR
                self.moshi_asr._process_audio_chunk(audio_data)
            else:
                return ErrorMessage(error_message="Format audio non supporté (attendu: bytes)")
            
            # Retourne un message de confirmation (optionnel)
            return OutputMessage(
                result="Audio chunk traité",
                metadata={
                    "chunk_size": len(audio_data),
                    "client_id": self.current_client_id,
                    "timestamp": time.time()
                }
            )
            
        except Exception as e:
            error_msg = f"Erreur traitement audio ASR: {e}"
            logger.error(error_msg)
            return ErrorMessage(
                error=e,
                error_message=error_msg
            )
    
    def reset_transcription(self):
        """Remet à zéro l'état de transcription"""
        try:
            if self.moshi_asr:
                self.moshi_asr.reset()
            
            self.text_buffer = []
            self.current_client_id = None
            
            logger.info("Transcription reset")
            
        except Exception as e:
            logger.error(f"Erreur reset transcription: {e}")
    
    def get_asr_stats(self):
        """Retourne les statistiques ASR"""
        stats = {
            "asr_active": self.moshi_asr is not None,
            "buffer_length": len(self.text_buffer),
            "current_client": self.current_client_id,
            "host": self.host
        }
        
        if self.moshi_asr:
            stats.update({
                "connected": self.moshi_asr._connected,
                "stream_active": self.moshi_asr._stream_active,
                "packets_sent": self.moshi_asr.packets_sent,
                "packets_received": self.moshi_asr.packets_received
            })
        
        return stats
    
    def cleanup(self):
        """Nettoie les ressources ASR"""
        print(f"Nettoyage Kyutai ASR {self.name}")
        
        # Arrête la ChunkQueue
        if hasattr(self, 'moshi_event_queue') and self.moshi_event_queue:
            self.moshi_event_queue.stop()
        
        # Nettoie MoshiASR
        if self.moshi_asr:
            try:
                self.moshi_asr.disconnect()
            except Exception as e:
                logger.error(f"Erreur déconnexion MoshiASR: {e}")
            finally:
                self.moshi_asr = None
        
        self.text_buffer = []
        self.current_client_id = None
        
        print(f"Kyutai ASR {self.name} nettoyé")