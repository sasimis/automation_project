import streamlit as st
from pathlib import Path
from weasyprint import HTML
import base64
import tempfile
import os

# Page configuration
st.set_page_config(
    page_title="HTML File Editor",
    page_icon="📄",
    layout="wide"
)

def html_to_pdf(html_content, filename):
    """Convert HTML content to PDF bytes"""
    try:
        # Create a temporary file to handle the HTML conversion
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as temp_html:
            temp_html.write(html_content)
            temp_html_path = temp_html.name
        
        # Convert HTML to PDF
        pdf_bytes = HTML(temp_html_path).write_pdf()
        
        # Clean up temporary file
        os.unlink(temp_html_path)
        
        return pdf_bytes
        
    except Exception as e:
        st.error(f"Error converting to PDF: {e}")
        return None

def main():
    st.title("📄 HTML File Editor")
    st.markdown("Edit HTML files and export as PDF")
    
    # File selection
    html_dir = st.text_input("HTML Files Directory", value="./data/invoices")
    
    if st.button("🔄 Refresh File List"):
        st.rerun()
    
    # Get HTML files
    html_files = []
    try:
        html_path = Path(html_dir)
        if html_path.exists() and html_path.is_dir():
            html_files = list(html_path.glob("*.html")) + list(html_path.glob("*.htm"))
        else:
            st.warning(f"Directory not found: {html_dir}")
    except Exception as e:
        st.error(f"Error accessing directory: {e}")
    
    if not html_files:
        st.info("No HTML files found in the specified directory.")
        return
    
    # File selection dropdown
    selected_file = st.selectbox(
        "Select HTML File to Edit",
        options=html_files,
        format_func=lambda x: x.name
    )
    
    if selected_file:
        try:
            with open(selected_file, 'r', encoding='utf-8') as f:
                html_content = f.read()
            
            # Display basic file information
            st.write(f"**📁 File:** {selected_file.name}")
            st.write(f"**📏 Size:** {len(html_content)} bytes")
            st.write(f"**📍 Path:** {selected_file}")
            st.markdown("---")
            
            # Source code editor
            st.markdown("### HTML Source Code")
            
            # Text area for editing HTML
            edited_content = st.text_area(
                "Edit HTML content:",
                value=html_content,
                height=500,
                key=f"editor_{selected_file.name}",
                help="Edit the raw HTML source code directly"
            )
            
            # Action buttons
            col1, col2, col3 = st.columns(3)
            
            with col1:
                if st.button("💾 Save Changes", type="primary", use_container_width=True):
                    try:
                        with open(selected_file, 'w', encoding='utf-8') as f:
                            f.write(edited_content)
                        st.success("✅ Changes saved successfully!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ Error saving file: {e}")
            
            with col2:
                # Export to PDF button
                if st.button("📄 Export to PDF", use_container_width=True):
                    pdf_bytes = html_to_pdf(edited_content, selected_file.name)
                    if pdf_bytes:
                        # Create download button for PDF
                        st.download_button(
                            label="⬇️ Download PDF",
                            data=pdf_bytes,
                            file_name=f"{selected_file.stem}.pdf",
                            mime="application/pdf",
                            use_container_width=True,
                            key=f"pdf_{selected_file.name}"
                        )
            
            with col3:
                if st.button("🔄 Reload File", use_container_width=True):
                    st.rerun()  
        except Exception as e:
            st.error(f"Error reading file: {e}")
if __name__ == "__main__":
    main()